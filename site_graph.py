import argparse
import copy
import html
import json
import pickle
import time
import urllib.parse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

import networkx as nx
import numpy as np
import requests
import scipy
from bs4 import BeautifulSoup
from jinja2 import ChoiceLoader, DictLoader
from pyvis.network import Network

from crawl_result import (
    CrawlNode, CrawlResult, FetchError, Observation, is_internal, load_data,
    utc_now, validate_crawl_data, write_json,
)

INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#FF9F40'
ERROR_COLOR = '#FF0800'
RESOURCE_COLOR = '#2ECC71'


def resolve_url(href, base_url='', site_url=None, keep_queries=True):
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, href.strip()))
        if parts.scheme not in ('http', 'https'):
            return None
        if not parts.hostname:
            raise ValueError('missing hostname')
        host = parts.hostname.lower()
        host = f'[{host}]' if ':' in host else host
        port = parts.port
        if port is not None and port != (443 if parts.scheme == 'https' else 80):
            host += f':{port}'
        if '@' in parts.netloc:
            host = parts.netloc.rsplit('@', 1)[0] + '@' + host
        url = parts._replace(netloc=host, path=parts.path or '/', fragment='').geturl()
        if not keep_queries and site_url is not None and is_internal(url, site_url):
            url = urllib.parse.urlsplit(url)._replace(query='').geturl()
        return url
    except ValueError as error:
        print(f'Ignoring invalid URL {href!r}: {error}')
        return None


def request_with_retries(session, method, url):
    for attempt in range(3):
        delay = 0.5 * 2 ** attempt
        try:
            response = session.request(
                method, url, allow_redirects=False, stream=True, timeout=(5, 10)
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 2:
                raise
        else:
            if attempt == 2 or response.status_code not in (429, 500, 502, 503, 504):
                return response
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                try:
                    delay = int(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        print(f'Ignoring invalid Retry-After header from {url}: {retry_after!r}')
                # Do not retry earlier than requested or stall indefinitely.
                if delay > 30:
                    return response
            response.close()
        time.sleep(max(0, delay))


@dataclass
class FetchResult:
    url: str
    status: Optional[int] = None
    error: Optional[str] = None
    is_html: bool = False
    text: Optional[str] = None
    redirects: tuple = ()
    observations: list[tuple[str, Observation]] = field(default_factory=list)


def fetch(session, url, discover=False, site_url=None):
    result = FetchResult(url)
    method = 'GET' if discover else 'HEAD'

    def fail(kind, message):
        result.error = message
        observation.fetch_error = FetchError(kind, message)

    try:
        while True:
            observation = Observation(method)
            result.observations.append((result.url, observation))
            result.status = None
            with request_with_retries(session, method, result.url) as response:
                result.status = response.status_code
                observation.http_status = result.status
                if method == 'HEAD' and result.status in (405, 501):
                    method = 'GET'
                    continue
                if response.is_redirect:
                    target = resolve_url(response.headers['Location'], result.url)
                    if target is None:
                        fail('redirect', 'Invalid or unsupported redirect destination')
                        return result
                    observation.redirect_to = target
                    if len(result.redirects) >= 10:
                        fail('redirect', 'Redirect limit exceeded')
                        return result
                    result.redirects += ((result.url, target),)
                    result.url = target
                    result.status = None
                    method = 'GET' if discover and (
                        site_url is None or is_internal(target, site_url)
                    ) else 'HEAD'
                    continue
                if 300 <= result.status < 400:
                    fail('redirect', 'Redirect response without a usable destination')
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                result.is_html = content_type in ('text/html', 'application/xhtml+xml')
                observation.type = 'page' if result.is_html else ('resource' if content_type else 'unknown')
                if discover and result.is_html and 200 <= result.status < 300 and (
                    site_url is None or is_internal(result.url, site_url)
                ):
                    result.text = response.text
                    observation.discovered = True
                return result
    except requests.exceptions.RequestException as error:
        kind = ('timeout' if isinstance(error, requests.exceptions.Timeout) else
                'connection' if isinstance(error, requests.exceptions.ConnectionError) else 'request')
        fail(kind, f'{type(error).__name__}: {error}')
        return result


def canonical_url(url, aliases):
    seen = set()
    while url in aliases and url not in seen:
        seen.add(url)
        url = aliases[url]
    return url


def check_external_batch(urls):
    with requests.Session() as session:
        results = []
        for url in urls:
            print('Checking', url)
            results.append(fetch(session, url))
        return results


def check_external_targets(targets, workers):
    if targets:
        count = min(workers, len(targets))
        batches = [targets[index::count] for index in range(count)]
        with ThreadPoolExecutor(max_workers=count) as executor:
            for results in executor.map(check_external_batch, batches):
                yield from results


def record_fetch(result, page, aliases):
    def node(url):
        if url not in result.nodes:
            result.nodes[url] = CrawlNode(
                url, 'internal' if is_internal(url, result.resolved_root_url) else 'external'
            )
        return result.nodes[url]

    for observed_url, observation in page.observations:
        node(observed_url).observations.append(observation)
        if observation.redirect_to is not None:
            node(observation.redirect_to)
    # A cookie-setting redirect can legitimately return to an earlier URL.
    aliases.pop(page.url, None)
    for source, _ in page.redirects:
        if source != page.url:
            aliases[source] = page.url
    error = page.error or (page.status if page.status is not None and page.status >= 400 else None)
    if error is not None:
        print(f'{error} ERROR while visiting {page.url}')


def canonicalize_result(result, aliases):
    result.edges = {(canonical_url(source, aliases), canonical_url(target, aliases))
                    for source, target in result.edges}
    for url, item in result.nodes.items():
        canonical = canonical_url(url, aliases)
        item.alias_of = canonical if canonical != url else None


def recheck_external(result, workers=None):
    """Check recorded canonical external edge targets, retaining discovery/history."""
    if result.is_legacy:
        raise ValueError('External rechecks require v1 JSON; legacy pickle has unknown crawl settings')
    validate_crawl_data(result.to_dict())
    workers = result.settings['workers'] if workers is None else workers
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be at least 1')
    updated = copy.deepcopy(result)
    aliases = {url: node.alias_of for url, node in updated.nodes.items() if node.alias_of is not None}
    targets = sorted({target for _, target in updated.edges
                      if updated.nodes[target].scope == 'external'})
    for page in check_external_targets(targets, workers):
        record_fetch(updated, page, aliases)
    canonicalize_result(updated, aliases)
    updated.generated_at = utc_now()
    validate_crawl_data(updated.to_dict())
    return updated


def crawl(url, visit_external, keep_queries, workers=4):
    """Compatibility API for callers expecting the original four-item tuple."""
    return crawl_site(url, visit_external, keep_queries, workers).legacy_data()


def crawl_site(url, visit_external, keep_queries, workers=4, *, generated_at=None):
    if workers < 1:
        raise ValueError('workers must be at least 1')
    url = resolve_url(url)
    if url is None:
        raise ValueError('site URL must be an absolute HTTP or HTTPS URL')
    visited = set()
    result = CrawlResult(url, url, {
        'visit_external': visit_external, 'keep_queries': keep_queries, 'workers': workers,
    })
    aliases = {}
    external_targets = set()

    def node(url):
        if url not in result.nodes:
            result.nodes[url] = CrawlNode(url, 'internal' if is_internal(url, site_url) else 'external')
        return result.nodes[url]

    def record(page):
        record_fetch(result, page, aliases)
        for source, _ in page.redirects:
            visited.add(source)
        visited.add(page.url)

    with requests.Session() as session:
        root = fetch(session, url, discover=True)
        if root.error or root.status >= 400 or root.text is None:
            raise ValueError(f'Cannot crawl root {root.url}: {root.error or root.status} (HTML required)')
        site_url = root.url
        result.resolved_root_url = site_url
        node(result.requested_root_url)
        node(site_url)
        to_visit = deque([site_url])
        scheduled = {site_url}

        while to_visit:
            url = canonical_url(to_visit.popleft(), aliases)
            if url in visited:
                continue
            print('Visiting', url)
            page = root if url == site_url else fetch(session, url, discover=True, site_url=site_url)
            record(page)
            if page.text is None:
                continue
            soup = BeautifulSoup(page.text, 'html.parser')
            base = soup.find('base', href=True)
            base_url = (resolve_url(base['href'], page.url) or page.url) if base else page.url
            for link in soup.find_all('a', href=True):
                target = resolve_url(link['href'], base_url, site_url, keep_queries)
                if target is None:
                    continue
                node(target)
                result.edges.add((page.url, target))
                target = canonical_url(target, aliases)
                if is_internal(target, site_url):
                    if target not in scheduled and target not in visited:
                        scheduled.add(target)
                        to_visit.append(target)
                else:
                    external_targets.add(target)

    if visit_external:
        targets = sorted({canonical_url(url, aliases) for url in external_targets} - visited)
        for page in check_external_targets(targets, workers):
            record(page)

    canonicalize_result(result, aliases)
    result.generated_at = generated_at if generated_at is not None else utc_now()
    return result


def get_node_info(nodes, result):
    node_info = []
    for node in nodes:
        item = result.nodes[node]
        if item.error() is not None:
            node_info.append(f'Error: {item.error()}')
        elif item.is_resource():
            node_info.append('resource')
        elif item.scope == 'internal':
            node_info.append('internal')
        else:
            node_info.append('external')
    return node_info


def url_link(url):
    text = html.escape(url, quote=True)
    try:
        parts = urllib.parse.urlsplit(url)
        safe = parts.scheme in ('http', 'https') and parts.hostname and not any(
            ord(character) < 32 for character in url
        )
        parts.port
    except ValueError:
        safe = False
    return f'<a href="{text}" rel="noopener noreferrer">{text}</a>' if safe else text


def exploration_info(item, aliases, generated_at):
    latest = item.observations[-1] if item.observations else None
    state = item.to_dict()['check_state']
    categories = []
    status = latest.http_status if latest else None
    reported = status if item.metadata_known else item.legacy_error
    if type(reported) is int and reported >= 400:
        categories.append('404' if reported == 404 else
                          'other-4xx' if reported < 500 else '5xx')
    if latest and latest.fetch_error:
        categories.append(latest.fetch_error.kind)
    elif not item.metadata_known and reported is not None and not categories:
        categories.append('legacy-error')
    if state == 'checked':
        state = ('success' if status is not None and 200 <= status < 300
                 and not categories else 'other-checked')
    kind = 'resource' if item.is_resource() else (
        latest.type if latest else 'unknown'
    )
    detail = [
        f'Scope: {item.scope}; type: {kind}',
        f'Check: {state}' + (' (legacy metadata unavailable)' if state == 'unknown' else ''),
        f'Latest HTTP status: {status if status is not None else "unknown"}',
        'Discovery: ' + ('unknown' if not item.metadata_known else
                         'yes' if any(obs.discovered for obs in item.observations) else 'no'),
        f'Crawl timestamp: {generated_at or "unknown"}',
    ]
    if item.error() is not None:
        detail.append(f'Error: {item.error()}')
    if latest and latest.fetch_error:
        detail.append(f'Fetch failure: {latest.fetch_error.kind}')
    detail.extend(f'Alias: {alias}' for alias in aliases)
    return {
        'scope': item.scope, 'resource': item.is_resource(), 'state': state,
        'errors': categories, 'aliases': aliases, 'detail': '\n'.join(detail),
    }


def prepare_graph_template(net, interactive):
    # PyVis already uses tojson for nodes/edges, but inserts options as raw JSON.
    net.templateEnv.filters['script_safe'] = lambda text: (
        text.replace('&', '\\u0026').replace('<', '\\u003c').replace('>', '\\u003e')
        .replace('\u2028', '\\u2028').replace('\u2029', '\\u2029')
    )
    template = net.templateEnv.loader.get_source(net.templateEnv, net.path)[0]
    template = template.replace('{{options|safe}}', '{{options|script_safe|safe}}')
    if interactive:
        controls = Path(__file__).with_name('interactive_controls.html').read_text(encoding='utf-8')
        template = template.replace('</body>', controls + '\n</body>')
    net.templateEnv.loader = ChoiceLoader([
        DictLoader({'site_graph.html': template}), net.templateEnv.loader,
    ])
    net.path = 'site_graph.html'


def visualize(result, args):
    G = nx.DiGraph()
    G.add_nodes_from(result.graph_nodes())
    G.add_edges_from(result.edges)

    if args.save_txt is not None or args.save_npz is not None:
        nodes = list(G.nodes())
        adj_matrix = nx.to_numpy_array(G, nodelist=nodes, dtype=int)

        if args.save_npz is not None:
            base_fname = args.save_npz.replace('.npz', '')
            scipy.sparse.save_npz(args.save_npz, scipy.sparse.coo_matrix(adj_matrix))
        else:
            base_fname = args.save_txt.replace('.txt', '')
            np.savetxt(args.save_txt, adj_matrix, fmt='%d')

        node_info = get_node_info(nodes, result)
        with open(base_fname + '_nodes.txt', 'w') as f:
            f.write('\n'.join([nodes[i] + '\t' + node_info[i] for i in range(len(nodes))]))

    net = Network(width=args.width, height=args.height, directed=True)
    net.from_nx(G)

    if args.show_buttons:
        net.show_buttons()
    elif args.options is not None:
        net.set_options(args.options)

    interactive = getattr(args, 'interactive_controls', False)
    aliases = {}
    if interactive:
        for item in result.nodes.values():
            if item.alias_of is not None:
                aliases.setdefault(item.alias_of, []).append(item.id)

    for node in net.nodes:
        item = result.nodes[node['id']]
        node['size'] = 15
        node['label'] = ''
        if item.scope == 'internal':
            node['color'] = INTERNAL_COLOR
            if item.is_resource():
                node['color'] = RESOURCE_COLOR
        else:
            node['color'] = EXTERNAL_COLOR

        if item.error() is not None:
            node['title'] = f'{html.escape(str(item.error()))} Error: {url_link(node["id"])}'
            
            if not args.only_404 or item.error() == 404:
                node['color'] = ERROR_COLOR
        else:
            node['title'] = url_link(node['id'])

        if interactive:
            info = exploration_info(item, sorted(aliases.get(item.id, [])), result.generated_at)
            node['crawl'] = info
            node['title'] = url_link(item.id) + '<br>' + html.escape(info['detail']).replace('\n', '<br>')
            if info['state'] in ('unchecked', 'unknown'):
                node['shape'] = 'triangle' if info['state'] == 'unchecked' else 'diamond'

    prepare_graph_template(net, interactive)
    net.save_graph(args.vis_file)


def summarize(result):
    nodes = [result.nodes[url] for url in result.graph_nodes()]
    lines = [f'Graph: {len(nodes)} canonical nodes, {len(result.edges)} directed edges, '
             f'{len(result.nodes) - len(nodes)} aliases.']
    if result.is_legacy:
        lines.append('Discovery: unknown (legacy pickle). HTTP health and check coverage: unknown.')
    else:
        discovery = result.to_dict()['discovery']
        reasons = Counter(item['reason'] for item in discovery['reasons'])
        detail = ', '.join(f'{reason}={count}' for reason, count in sorted(reasons.items()))
        lines.append('Discovery: ' + ('complete.' if discovery['complete'] else f'incomplete ({detail}).'))
        checked = sum(bool(node.observations) for node in nodes)
        categories = Counter()
        failures = 0
        for node in nodes:
            if node.observations:
                latest = node.observations[-1]
                if latest.http_status is not None:
                    categories[latest.http_status // 100] += 1
                failures += latest.fetch_error is not None
        lines.append(f'Checking (canonical): {checked} checked, {len(nodes) - checked} unchecked; '
                     f'{failures} fetch failures.')
        lines.append('Latest HTTP responses: ' + ', '.join(
            f'{category}xx={categories[category]}' for category in range(1, 6)
        ) + '. HTTP responses and fetch failures can overlap.')
    broken = [node for node in nodes if node.error() is not None]
    lines.append(f'{"Legacy reported errors" if result.is_legacy else "Broken targets"}: {len(broken)}.')
    referrers = {}
    for source, target in sorted(result.edges):
        referrers.setdefault(target, []).append(source)
    for node in broken:
        lines.append(f'  {node.id}: {node.error()}')
        lines.append('    Referred by: ' + (', '.join(referrers.get(node.id, [])) or '(none recorded)'))
    return '\n'.join(lines)


def read_options(filename):
    text = Path(filename).read_text(encoding='utf-8').strip()
    if text.startswith('var options ='):
        text = text[len('var options ='):].strip().rstrip(';')
    options = json.loads(text)
    if not isinstance(options, dict):
        raise ValueError('drawing options must be a JSON object')
    # vis-network accepts a boolean, but pyvis's HTML generator needs an object.
    if type(options.get('physics')) is bool:
        options['physics'] = {'enabled': options['physics']}
    for key, value in options.items():
        if key in ('nodes', 'edges', 'layout', 'interaction', 'manipulation', 'physics') and not (
                isinstance(value, dict) or (key == 'manipulation' and type(value) is bool)):
            raise ValueError(f'invalid drawing options: {key}')
    if 'enabled' in options.get('physics', {}) and type(options['physics']['enabled']) is not bool:
        raise ValueError('invalid drawing options: physics.enabled must be boolean')
    return json.dumps(options)


def main():
    parser = argparse.ArgumentParser(description='Visualize the link graph of a website.')
    parser.add_argument('site_url', type=str, help='the base URL of the website', nargs='?', default='')

    # Defaults
    vis_file = 'site.html'
    data_file = 'crawl.pickle'
    width = 1000
    height = 800

    parser.add_argument('--vis-file', type=str, help=f'filename in which to save HTML graph visualization (default: {vis_file})', default=vis_file)
    parser.add_argument('--data-file', type=str, help=f'filename in which to save crawled graph data (default: {data_file})', default=data_file)
    parser.add_argument('--json-file', type=str, help='also save versioned crawl JSON to this file')
    parser.add_argument('--width', type=int, help=f'width of graph visualization in pixels (default: {width})', default=width)
    parser.add_argument('--height', type=int, help=f'height of graph visualization in pixels (default: {height})', default=height)
    parser.add_argument('--visit-external', action='store_true', help='detect broken external links (slower)')
    parser.add_argument('--workers', type=int, help='parallel external link checks (default: 4, or saved setting)')
    parser.add_argument('--show-buttons', action='store_true', help='show visualization settings UI')
    parser.add_argument('--interactive-controls', action='store_true', help='add client-side URL search, filters, legend and directed neighbor selection')
    parser.add_argument('--options', type=str, help='file with drawing options (use --show-buttons to configure, then generate options)')
    parser.add_argument('--from-data-file', type=str, help='render saved JSON or trusted .pickle/.pkl without network requests')
    parser.add_argument('--recheck-external', action='store_true', help='with saved JSON, check recorded external targets again (network)')
    parser.add_argument('--force', action='store_true', help='override warnings about base URL')
    parser.add_argument('--save-txt', type=str, nargs='?', help='filename in which to save adjacency matrix (if no argument, uses adj_matrix.txt). Also saves node labels to [filename]_nodes.txt', const='adj_matrix.txt', default=None)
    parser.add_argument('--save-npz', type=str, nargs='?', help='filename in which to save sparse adjacency matrix (if no argument, uses adj_matrix.npz). Also saves node labels to [filename]_nodes.txt',  const='adj_matrix.npz', default=None)
    parser.add_argument('--keep-queries',  action='store_true', help='keep query strings on internal links (fragments are always removed)')
    parser.add_argument('--only-404', action='store_true', help='only color 404 error nodes in the error color')

    args = parser.parse_args()
    if args.workers is not None and args.workers < 1:
        parser.error('--workers must be at least 1')
    if args.width < 1 or args.height < 1:
        parser.error('--width and --height must be positive')
    if not args.vis_file.endswith('.html'):
        parser.error('--vis-file must end in .html')
    if args.show_buttons and args.options:
        parser.error('--show-buttons and --options cannot be combined')
    if args.save_txt and args.save_npz:
        parser.error('choose --save-txt or --save-npz, not both')
    if args.recheck_external and not args.from_data_file:
        parser.error('--recheck-external requires --from-data-file')
    if args.from_data_file and (args.site_url or args.visit_external or args.keep_queries):
        parser.error('saved data defines root/scope/query policy; use --recheck-external for network checks')

    outputs = [args.vis_file]
    if not args.from_data_file:
        outputs.append(args.data_file)
    if args.json_file:
        outputs.append(args.json_file)
    if args.save_npz:
        outputs += [args.save_npz if args.save_npz.endswith('.npz') else args.save_npz + '.npz',
                    args.save_npz.replace('.npz', '') + '_nodes.txt']
    elif args.save_txt:
        outputs += [args.save_txt, args.save_txt.replace('.txt', '') + '_nodes.txt']
    inputs = [value for value in (args.options, args.from_data_file) if value]
    paths = [Path(value).resolve() for value in outputs + inputs]
    if len(paths) != len(set(paths)):
        parser.error('input and output files must have distinct paths')
    for filename in outputs:
        path = Path(filename)
        if not path.parent.is_dir() or path.is_dir():
            parser.error(f'invalid output path: {filename}')
    try:
        if args.options is not None:
            args.options = read_options(args.options)
    except (OSError, ValueError) as error:
        parser.error(f'Cannot load drawing options: {error}')

    try:
        if args.from_data_file is None:
            site_url = resolve_url(args.site_url)
            if site_url is None:
                parser.error('site_url must be an absolute HTTP or HTTPS URL')
            if urllib.parse.urlsplit(site_url).scheme != 'https' and not args.force:
                parser.error('not using https; use --force to allow http')
            result = crawl_site(
                site_url, args.visit_external, args.keep_queries, args.workers or 4
            )
            with open(args.data_file, 'wb') as f:
                pickle.dump(result.legacy_data(), f)
            print(f'Saved crawl data to {args.data_file}')
        else:
            result = load_data(args.from_data_file)
            if result.is_legacy and (args.json_file or args.recheck_external):
                parser.error('legacy pickle has unknown crawl metadata; JSON export/recheck requires v1 JSON')
            if args.recheck_external:
                result = recheck_external(result, args.workers)
                if args.json_file is None:
                    print('External checks updated in memory only; use --json-file to save them.')
        if args.json_file is not None:
            write_json(result, args.json_file)
            print(f'Saved crawl JSON to {args.json_file}')
        print(summarize(result))
        visualize(result, args)
    except (ValueError, OSError) as error:
        parser.exit(1, f'Error: {error}\n')
    print('Saved graph to', args.vis_file)


if __name__ == '__main__':
    main()
