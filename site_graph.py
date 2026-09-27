import argparse
import pickle
import time
import urllib.parse
from collections import deque
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
from pyvis.network import Network

from crawl_result import CrawlNode, CrawlResult, FetchError, Observation, is_internal, utc_now, write_json

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
        for observed_url, observation in page.observations:
            node(observed_url).observations.append(observation)
            if observation.redirect_to is not None:
                node(observation.redirect_to)
        # A cookie-setting redirect can legitimately return to an earlier URL.
        aliases.pop(page.url, None)
        for source, _ in page.redirects:
            if source != page.url:
                aliases[source] = page.url
            visited.add(source)
        visited.add(page.url)
        error = page.error or (page.status if page.status is not None and page.status >= 400 else None)
        if error is not None:
            print(f'{error} ERROR while visiting {page.url}')

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
        if targets:
            count = min(workers, len(targets))
            batches = [targets[index::count] for index in range(count)]
            with ThreadPoolExecutor(max_workers=count) as executor:
                for results in executor.map(check_external_batch, batches):
                    for page in results:
                        record(page)

    result.edges = {(canonical_url(source, aliases), canonical_url(target, aliases))
                    for source, target in result.edges}
    for url, item in result.nodes.items():
        canonical = canonical_url(url, aliases)
        item.alias_of = canonical if canonical != url else None
    result.generated_at = generated_at if generated_at is not None else utc_now()
    return result


def get_node_info(nodes, error_codes, resource_pages, args):
    node_info = []
    for node in nodes:
        if node in error_codes:
            node_info.append(f'Error: {error_codes[node]}')
        elif node in resource_pages:
            node_info.append('resource')
        elif is_internal(node, args.site_url):
            node_info.append('internal')
        else:
            node_info.append('external')
    return node_info


def visualize(edges, error_codes, resource_pages, args, nodes=None):
    G = nx.DiGraph()
    G.add_node(args.site_url)
    if nodes is not None:
        G.add_nodes_from(nodes)
    G.add_edges_from(edges)

    if args.save_txt is not None or args.save_npz is not None:
        nodes = list(G.nodes())
        adj_matrix = nx.to_numpy_array(G, nodelist=nodes, dtype=int)

        if args.save_npz is not None:
            base_fname = args.save_npz.replace('.npz', '')
            scipy.sparse.save_npz(args.save_npz, scipy.sparse.coo_matrix(adj_matrix))
        else:
            base_fname = args.save_txt.replace('.txt', '')
            np.savetxt(args.save_txt, adj_matrix, fmt='%d')

        node_info = get_node_info(nodes, error_codes, resource_pages, args)
        with open(base_fname + '_nodes.txt', 'w') as f:
            f.write('\n'.join([nodes[i] + '\t' + node_info[i] for i in range(len(nodes))]))

    net = Network(width=args.width, height=args.height, directed=True)
    net.from_nx(G)

    if args.show_buttons:
        net.show_buttons()
    elif args.options is not None:
        try:
            with open(args.options, 'r') as f:
                net.set_options(f.read())
        except FileNotFoundError as e:
            print('Error: options file', args.options, 'not found.')
        except Exception as e:
            print('Error applying options:', e)

    for node in net.nodes:
        node['size'] = 15
        node['label'] = ''
        if is_internal(node['id'], args.site_url):
            node['color'] = INTERNAL_COLOR
            if node['id'] in resource_pages:
                node['color'] = RESOURCE_COLOR
        else:
            node['color'] = EXTERNAL_COLOR

        if node['id'] in error_codes:
            node['title'] = f'{error_codes[node["id"]]} Error: <a href="{node["id"]}">{node["id"]}</a>'
            
            if not args.only_404 or error_codes[node['id']] == 404:
                node['color'] = ERROR_COLOR
        else:
            node['title'] = f'<a href="{node["id"]}">{node["id"]}</a>'
    
    net.save_graph(args.vis_file)


if __name__ == '__main__':
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
    parser.add_argument('--workers', type=int, default=4, help='parallel external link checks (default: 4; use 1 for serial checks)')
    parser.add_argument('--show-buttons', action='store_true', help='show visualization settings UI')
    parser.add_argument('--options', type=str, help='file with drawing options (use --show-buttons to configure, then generate options)')
    parser.add_argument('--from-data-file', type=str, help='create visualization from given data file', default=None)
    parser.add_argument('--force', action='store_true', help='override warnings about base URL')
    parser.add_argument('--save-txt', type=str, nargs='?', help='filename in which to save adjacency matrix (if no argument, uses adj_matrix.txt). Also saves node labels to [filename]_nodes.txt', const='adj_matrix.txt', default=None)
    parser.add_argument('--save-npz', type=str, nargs='?', help='filename in which to save sparse adjacency matrix (if no argument, uses adj_matrix.npz). Also saves node labels to [filename]_nodes.txt',  const='adj_matrix.npz', default=None)
    parser.add_argument('--keep-queries',  action='store_true', help='keep query strings on internal links (fragments are always removed)')
    parser.add_argument('--only-404', action='store_true', help='only color 404 error nodes in the error color')

    args = parser.parse_args()
    if args.workers < 1:
        parser.error('--workers must be at least 1')
    if args.json_file is not None and args.from_data_file is not None:
        parser.error('--json-file requires a new crawl; legacy pickle lacks fetch observations')
    if args.json_file is not None:
        outputs = [args.data_file, args.vis_file]
        if args.options is not None:
            outputs.append(args.options)
        if args.save_npz is not None:
            matrix_file = args.save_npz if args.save_npz.endswith('.npz') else args.save_npz + '.npz'
            outputs += [matrix_file, args.save_npz.replace('.npz', '') + '_nodes.txt']
        elif args.save_txt is not None:
            outputs += [args.save_txt, args.save_txt.replace('.txt', '') + '_nodes.txt']
        if Path(args.json_file).resolve() in {Path(output).resolve() for output in outputs}:
            parser.error('--json-file must differ from other input/output files')

    nodes = None
    if args.from_data_file is None:
        site_url = resolve_url(args.site_url)
        if site_url is None:
            parser.error('site_url must be an absolute HTTP or HTTPS URL')
        if urllib.parse.urlsplit(site_url).scheme != 'https':
            if not args.force:
                print('Warning: not using https. If you really want to use http, run with --force')
                exit(1)

        try:
            result = crawl_site(
                site_url, args.visit_external, args.keep_queries, args.workers
            )
            edges, error_codes, resource_pages, args.site_url = result.legacy_data()
            nodes = result.graph_nodes()
            if args.json_file is not None:
                write_json(result, args.json_file)
                print(f'Saved crawl JSON to {args.json_file}')
        except ValueError as error:
            parser.exit(1, f'Error: {error}\n')
        except OSError as error:
            parser.exit(1, f'Error saving crawl JSON: {error}\n')
        print('Crawl complete.')

        with open(args.data_file, 'wb') as f:
            pickle.dump((edges, error_codes, resource_pages, args.site_url), f)
            print(f'Saved crawl data to {args.data_file}')
    else:
        with open(args.from_data_file, 'rb') as f:
            edges, error_codes, resource_pages, site_url = pickle.load(f)
            args.site_url = site_url

    visualize(edges, error_codes, resource_pages, args, nodes=nodes)
    print('Saved graph to', args.vis_file)
