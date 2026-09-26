import argparse
import pickle
import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Optional

import networkx as nx
import numpy as np
import requests
import scipy
from bs4 import BeautifulSoup
from pyvis.network import Network

INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#FF9F40'
ERROR_COLOR = '#FF0800'
RESOURCE_COLOR = '#2ECC71'


def is_internal(url, site_url):
    target = urllib.parse.urlsplit(url)
    root = urllib.parse.urlsplit(site_url)

    def origin(parts):
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        return parts.scheme, parts.hostname, port

    path = root.path.rstrip('/')
    return origin(target) == origin(root) and (
        target.path == path or target.path.startswith(path + '/')
    )


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


def fetch(session, url, discover=False, site_url=None):
    result = FetchResult(url)
    method = 'GET' if discover else 'HEAD'
    try:
        while True:
            with request_with_retries(session, method, result.url) as response:
                result.status = response.status_code
                if method == 'HEAD' and result.status in (405, 501):
                    method = 'GET'
                    continue
                if response.is_redirect:
                    target = resolve_url(response.headers['Location'], result.url)
                    if target is None:
                        result.error = 'Invalid or unsupported redirect destination'
                        return result
                    if len(result.redirects) >= 10:
                        result.error = 'Redirect limit exceeded'
                        return result
                    result.redirects += ((result.url, target),)
                    result.url = target
                    result.status = None
                    method = 'GET' if discover and (
                        site_url is None or is_internal(target, site_url)
                    ) else 'HEAD'
                    continue
                if 300 <= result.status < 400:
                    result.error = 'Redirect response without a usable destination'
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                result.is_html = content_type in ('text/html', 'application/xhtml+xml')
                if discover and result.is_html and 200 <= result.status < 300 and (
                    site_url is None or is_internal(result.url, site_url)
                ):
                    result.text = response.text
                return result
    except requests.exceptions.RequestException as error:
        result.error = f'{type(error).__name__}: {error}'
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
    if workers < 1:
        raise ValueError('workers must be at least 1')
    url = resolve_url(url)
    if url is None:
        raise ValueError('site URL must be an absolute HTTP or HTTPS URL')
    visited = set()
    edges = set()
    resource_pages = set()
    error_codes = dict()
    aliases = {}
    external_targets = set()

    def record(result):
        # A cookie-setting redirect can legitimately return to an earlier URL.
        aliases.pop(result.url, None)
        for source, _ in result.redirects:
            if source != result.url:
                aliases[source] = result.url
                error_codes.pop(source, None)
                resource_pages.discard(source)
            visited.add(source)
        visited.add(result.url)
        resource_pages.discard(result.url)
        error = result.error or (result.status if result.status >= 400 else None)
        if error is not None:
            error_codes[result.url] = error
            print(f'{error} ERROR while visiting {result.url}')
        else:
            error_codes.pop(result.url, None)
            if not result.is_html:
                resource_pages.add(result.url)

    with requests.Session() as session:
        root = fetch(session, url, discover=True)
        if root.error or root.status >= 400 or root.text is None:
            raise ValueError(f'Cannot crawl root {root.url}: {root.error or root.status} (HTML required)')
        site_url = root.url
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
                edges.add((page.url, target))
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
                    for result in results:
                        record(result)

    edges = {(canonical_url(source, aliases), canonical_url(target, aliases))
             for source, target in edges}
    error_codes = {canonical_url(url, aliases): error for url, error in error_codes.items()}
    resource_pages = {canonical_url(url, aliases) for url in resource_pages}
    return edges, error_codes, resource_pages, site_url


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


def visualize(edges, error_codes, resource_pages, args):
    G = nx.DiGraph()
    G.add_node(args.site_url)
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

    if args.from_data_file is None:
        site_url = resolve_url(args.site_url)
        if site_url is None:
            parser.error('site_url must be an absolute HTTP or HTTPS URL')
        if urllib.parse.urlsplit(site_url).scheme != 'https':
            if not args.force:
                print('Warning: not using https. If you really want to use http, run with --force')
                exit(1)

        try:
            edges, error_codes, resource_pages, args.site_url = crawl(
                site_url, args.visit_external, args.keep_queries, args.workers
            )
        except ValueError as error:
            parser.exit(1, f'Error: {error}\n')
        print('Crawl complete.')

        with open(args.data_file, 'wb') as f:
            pickle.dump((edges, error_codes, resource_pages, args.site_url), f)
            print(f'Saved crawl data to {args.data_file}')
    else:
        with open(args.from_data_file, 'rb') as f:
            edges, error_codes, resource_pages, site_url = pickle.load(f)
            args.site_url = site_url

    visualize(edges, error_codes, resource_pages, args)
    print('Saved graph to', args.vis_file)
