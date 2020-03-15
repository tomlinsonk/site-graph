from bs4 import BeautifulSoup
import urllib
import requests
from pyvis.network import Network
import networkx as nx
import argparse

from queue import Queue

INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#F45B69'
ERROR_COLOR = '#FFE74C'
RESOURCE_COLOR = '#2ECC71'


def crawl(url, visit_external):
    visited = set()
    edges = set()
    resouce_pages = set()
    error_codes = dict()

    to_visit = Queue()
    to_visit.put((url, None))

    site_url = url

    while not to_visit.empty():
        url, from_url = to_visit.get()

        print('Visiting', url, 'from', from_url)
        is_html = False
        error = False
        error_obj = None

        try:
            head = requests.head(url, timeout=10)
            if head and 'html' in head.headers.get('content-type', ''):
                page = requests.get(url, timeout=10)
                is_html = True
        except requests.exceptions.RequestException as e:
            error = True
            error_obj = e

        if error or not head or (is_html and not page):
            error = str(error_obj) if error else head.status_code
            visited.add(url)
            error_codes[url] = error
            edges.add((from_url, url))
            print(f'{error} ERROR while visitng {url}')
            continue

        # Handle redirects and get consistent URL
        url = head.url

        if from_url is not None:
            edges.add((from_url, url))

        if not is_html:
            resouce_pages.add(url)
        
        if url in visited:
            continue
        
        visited.add(url)
        
        if not url.startswith(site_url) or not is_html:
            continue
        
        soup = BeautifulSoup(page.text, 'html.parser', from_encoding=page.apparent_encoding)
        internal_links = set()
        external_links = set()
        for link in soup.find_all('a', href=True):
            link_url = link['href']
        
            if link_url.startswith('mailto:'):
                continue
            
            # Resolve relative paths
            if not link_url.startswith('http'):
                link_url = urllib.parse.urljoin(url, link_url)

            # Remove queries/fragments
            link_url = urllib.parse.urljoin(link_url, urllib.parse.urlparse(link_url).path)

            if link_url not in visited and (visit_external or link_url.startswith(site_url)):
                to_visit.put((link_url, url))
            else:
                edges.add((url, link_url))


    return edges, error_codes, resouce_pages


def visualize(edges, error_codes, resouce_pages, args):
    G = nx.DiGraph()
    G.add_edges_from(edges)

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
        if node['title'].startswith(args.site_url):
            node['color'] = INTERNAL_COLOR
            if node['title'] in resouce_pages:
                node['color'] = RESOURCE_COLOR
        else:
            node['color'] = EXTERNAL_COLOR

        if node['title'] in error_codes:
            node['title'] = f'{error_codes[node["title"]]}Error: <a href="{node["title"]}">{node["title"]}</a>'
            node['color'] = ERROR_COLOR
        else:
            node['title'] = f'<a href="{node["title"]}">{node["title"]}</a>'

    net.save_graph(args.out_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Visualize the link graph of a website.')
    parser.add_argument('site_url', type=str, help='the base URL of the website')
    parser.add_argument('--out-file', type=str, help='filename in which to save HTML graph visualization (default: site.html)', default='site.html')
    parser.add_argument('--width', type=int, help='width of graph visualization in pixels (default: 800)', default=1000)
    parser.add_argument('--height', type=int, help='height of graph visualization in pixels (default: 800)', default=800)
    parser.add_argument('--visit-external', action='store_true', help='detect broken external links (slower)')
    parser.add_argument('--show-buttons', action='store_true', help='show visualization settings UI')
    parser.add_argument('--options', type=str, help='file with drawing options (use --show-buttons to configure, then generate options)')

    args = parser.parse_args()

    if not args.site_url.endswith('/'):
        print('Warning: no trailing slash on site_url (may get duplicate homepage node)')

    if not args.site_url.startswith('https'):
        print('Warning: not using https')

    edges, error_codes, resouce_pages = crawl(args.site_url, args.visit_external)
    print('Crawl complete.')

    visualize(edges, error_codes, resouce_pages, args)
    print('Saved graph to', args.out_file)
