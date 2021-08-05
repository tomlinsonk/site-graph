from bs4 import BeautifulSoup
import urllib
import requests
from pyvis.network import Network
import networkx as nx
import argparse
import pickle
import scipy
import numpy as np

from collections import deque

INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#F45B69'
ERROR_COLOR = '#FFE74C'
RESOURCE_COLOR = '#2ECC71'


def handle_error(error, error_obj, r, url, visited, error_codes):
    error = str(error_obj) if error else r.status_code
    visited.add(url)
    error_codes[url] = error
    print(f'{error} ERROR while visitng {url}')


def crawl(url, visit_external):
    visited = set()
    edges = set()
    resouce_pages = set()
    error_codes = dict()
    canonical_urls = dict() 

    head = requests.head(url, timeout=10)
    site_url = head.url
    canonical_urls[url] = site_url

    to_visit = deque()
    to_visit.append((site_url, None))

    while to_visit:
        url, from_url = to_visit.pop()

        print('Visiting', url, 'from', from_url)

        error = False
        error_obj = None
        try:
            page = requests.get(url, timeout=10)
        except requests.exceptions.RequestException as e:
            error = True
            error_obj = e

        if error or not page:
            handle_error(error, error_obj, page, url, visited, error_codes)
            continue
        
        soup = BeautifulSoup(page.text, 'html.parser')
        internal_links = set()
        external_links = set()

        # Handle <base> tags
        base_url = soup.find('base')
        base_url = '' if base_url is None else base_url.get('href', '')

        for link in soup.find_all('a', href=True):
            link_url = link['href']
        
            if link_url.startswith('mailto:'):
                continue
            
            # Resolve relative paths
            if not link_url.startswith('http'):
                link_url = urllib.parse.urljoin(url, urllib.parse.urljoin(base_url, link_url))

            # Remove queries/fragments from internal links
            if link_url.startswith(site_url):
                link_url = urllib.parse.urljoin(link_url, urllib.parse.urlparse(link_url).path)

            # Load canonical version of link_url
            if link_url in canonical_urls:
                link_url = canonical_urls[link_url]

            if link_url not in visited and (visit_external or link_url.startswith(site_url)):
                is_html = False
                error = False
                error_obj = None

                try:
                    head = requests.head(link_url, timeout=10)
                    if head and 'html' in head.headers.get('content-type', ''):
                        is_html = True
                except requests.exceptions.RequestException as e:
                    error = True
                    error_obj = e

                if error or not head:
                    handle_error(error, error_obj, head, link_url, visited, error_codes)
                    edges.add((url, link_url))
                    continue

                canonical_urls[link_url] = head.url
                link_url = head.url
                visited.add(link_url)

                if link_url.startswith(site_url):
                    if is_html:
                        to_visit.append((head.url, url))
                    else:
                        resouce_pages.add(link_url)
            
            edges.add((url, link_url))

    return edges, error_codes, resouce_pages


def get_node_info(nodes, error_codes, resource_pages, args):
    node_info = []
    for node in nodes:
        if node in error_codes:
            node_info.append(f'Error: {error_codes[node]}')
        elif node in resource_pages:
            node_info.append('resource')
        elif node.startswith(args.site_url):
            node_info.append('internal')
        else:
            node_info.append('external')
    return node_info


def visualize(edges, error_codes, resouce_pages, args):
    G = nx.DiGraph()
    G.add_edges_from(edges)

    if args.save_txt is not None or args.save_npz is not None:
        nodes = list(G.nodes())
        adj_matrix = nx.to_numpy_matrix(G, nodelist=nodes, dtype=int)

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
        if node['id'].startswith(args.site_url):
            node['color'] = INTERNAL_COLOR
            if node['id'] in resouce_pages:
                node['color'] = RESOURCE_COLOR
        else:
            node['color'] = EXTERNAL_COLOR

        if node['id'] in error_codes:
            node['title'] = f'{error_codes[node["id"]]} Error: <a href="{node["id"]}">{node["id"]}</a>'
            node['color'] = ERROR_COLOR
        else:
            node['title'] = f'<a href="{node["id"]}">{node["id"]}</a>'

    net.save_graph(args.vis_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Visualize the link graph of a website.')
    parser.add_argument('site_url', type=str, help='the base URL of the website', nargs='?', default='')
    parser.add_argument('--vis-file', type=str, help='filename in which to save HTML graph visualization (default: site.html)', default='site.html')
    default_filename = 'crawl.pickle'
    parser.add_argument('--data-file', type=str, help='filename in which to save crawled graph data (default: ' + default_filename + ')', default=default_filename)
    parser.add_argument('--width', type=int, help='width of graph visualization in pixels (default: 800)', default=1000)
    parser.add_argument('--height', type=int, help='height of graph visualization in pixels (default: 800)', default=800)
    parser.add_argument('--visit-external', action='store_true', help='detect broken external links (slower)')
    parser.add_argument('--show-buttons', action='store_true', help='show visualization settings UI')
    parser.add_argument('--options', type=str, help='file with drawing options (use --show-buttons to configure, then generate options)')
    parser.add_argument('--from-data-file', type=str, help='create visualization from given data file', default=None)
    parser.add_argument('--force', action='store_true', help='override warnings about base URL')
    parser.add_argument('--save-txt', type=str, nargs='?', help='filename in which to save adjacency matrix (if no argument, uses adj_matrix.txt). Also saves node labels to [filename]_nodes.txt', const='adj_matrix.txt', default=None)
    parser.add_argument('--save-npz', type=str, nargs='?', help='filename in which to save sparse adjacency matrix (if no argument, uses adj_matrix.npz). Also saves node labels to [filename]_nodes.txt',  const='adj_matrix.npz', default=None)

    args = parser.parse_args()

    if args.from_data_file is None:
        if not args.site_url.endswith('/'):
            print('Warning: no trailing slash on site_url (may get duplicate homepage node). If you really don\'t want the trailing slash, run with --force')
            if not args.force:
                exit(1)

        if not args.site_url.startswith('https'):
            print('Warning: not using https. If you really want to use http, run with --force')
            if not args.force:
                exit(1)

        edges, error_codes, resource_pages = crawl(args.site_url, args.visit_external)
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
