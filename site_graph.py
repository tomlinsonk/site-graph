from bs4 import BeautifulSoup
import urllib
from pyvis.network import Network
import networkx as nx
import argparse


INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#F45B69'
ERROR_COLOR = '#FFE74C'


def crawl(url, visit_external):
    visited = set()
    edges = set()
    error_pages = set()

    crawl_helper(url, None, visited, edges, error_pages, url, visit_external)

    return edges, error_pages


def crawl_helper(url, from_url, visited, edges, error_pages, site_url, visit_external):
    print('Visiting', url, 'from', from_url) 
    try:
        resp = urllib.request.urlopen(url)
    except Exception as e:
        visited.add(url)
        error_pages.add(url)
        edges.add((from_url, url))
        print('Error visiting', url,  e)
        return
    
    # Handle redirects and get consistent URL
    url = resp.geturl()
    
    if from_url is not None:
        edges.add((from_url, url))
    
    if url in visited:
        return
    
    visited.add(url)
    
    if not url.startswith(site_url) or url.endswith('.pdf'):
        return
    
    soup = BeautifulSoup(resp, 'html.parser', from_encoding=resp.info().get_param('charset'))
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
            crawl_helper(link_url, url, visited, edges, error_pages, site_url, visit_external)
        else:
            edges.add((url, link_url))


def visualize(out_file, edges, error_pages, width, height, show_buttons, site_url):
    G = nx.DiGraph()
    G.add_edges_from(edges)

    print(width, height)
    net = Network(width=width, height=height, directed=True)
    net.from_nx(G)

    if show_buttons:
        net.show_buttons()
    else:
        net.set_options('{"edges": {"arrows": {"to": {"scaleFactor": 0.5}}}}')

    for node in net.nodes:
        node['size'] = 15
        node['label'] = ''
        if node['title'].startswith(site_url):
            node['color'] = INTERNAL_COLOR
        else:
            node['color'] = EXTERNAL_COLOR
        if node['title'] in error_pages:
            node['color'] = ERROR_COLOR
            
        node['title'] = f'<a href="{node["title"]}">{node["title"]}</a>'
        
    net.save_graph(out_file)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Visualize the link graph of a website.')
    parser.add_argument('site_url', type=str, help='the base URL of the website')
    parser.add_argument('--out-file', type=str, help='filename in which to save HTML graph visualization (default: site.html)', default='site.html')
    parser.add_argument('--width', type=int, help='width of graph visualization in pixels (default: 800)', default=1000)
    parser.add_argument('--height', type=int, help='height of graph visualization in pixels (default: 800)', default=800)
    parser.add_argument('--visit-external', action='store_true', help='detect broken external links (slower)')
    parser.add_argument('--show-buttons', action='store_true', help='show visualization settings UI')

    args = parser.parse_args()

    if not args.site_url.endswith('/'):
        print('Warning: no trailing slash on site_url (may get duplicate homepage node)')

    if not args.site_url.startswith('https'):
        print('Warning: not using https')

    edges, error_pages = crawl(args.site_url, args.visit_external)
    print('Crawl complete.')

    visualize(args.out_file, edges, error_pages, args.width, args.height, args.show_buttons, args.site_url)
    print('Saved graph to', args.out_file)
