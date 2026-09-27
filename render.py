"""Graph presentation: HTML controls, drawing options, and matrix exports."""

import html
import json
import urllib.parse
from pathlib import Path

import networkx as nx
import numpy as np
from jinja2 import ChoiceLoader, DictLoader
from pyvis.network import Network
from scipy import sparse

INTERNAL_COLOR = '#0072BB'
EXTERNAL_COLOR = '#FF9F40'
ERROR_COLOR = '#FF0800'
RESOURCE_COLOR = '#2ECC71'


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
    latest = item.latest
    status = latest.http_status if latest else None
    categories = []
    if status is not None and status >= 400:
        categories.append('404' if status == 404 else 'other-4xx' if status < 500 else '5xx')
    if latest and latest.fetch_error:
        categories.append(latest.fetch_error.kind)
    state = ('unchecked' if latest is None else
             'success' if status is not None and 200 <= status < 300 and not categories
             else 'other-checked')
    detail = [
        f'Scope: {item.scope}; type: {latest.type if latest else "unknown"}',
        f'Check: {state}',
        f'Latest HTTP status: {status if status is not None else "unknown"}',
        'Discovery: ' + ('yes' if any(obs.discovered for obs in item.observations) else 'no'),
        f'Crawl timestamp: {generated_at}',
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
    # Replace PyVis's page-positioned loading CSS, markup and event handlers.
    template = template.replace(
        '{% if nodes|length > 100 and physics_enabled %}', '{% if false %}',
    )
    for name in ['graph_layout.html'] + (['interactive_controls.html'] if interactive else []):
        content = Path(__file__).with_name(name).read_text(encoding='utf-8')
        template = template.replace('</body>', content + '\n</body>')
    net.templateEnv.loader = ChoiceLoader([
        DictLoader({'site_graph.html': template}), net.templateEnv.loader,
    ])
    net.path = 'site_graph.html'


def visualize(result, args):
    graph = nx.DiGraph()
    graph.add_nodes_from(result.graph_nodes())
    graph.add_edges_from(sorted(result.edges))
    matrix_file = args.save_npz or args.save_txt
    if matrix_file:
        matrix = nx.to_numpy_array(graph, dtype=int)
        if args.save_npz:
            sparse.save_npz(matrix_file, sparse.coo_matrix(matrix))
        else:
            np.savetxt(matrix_file, matrix, fmt='%d')
        labels = []
        for url in graph:
            item = result.nodes[url]
            info = (f'Error: {item.error()}' if item.error() is not None else
                    'resource' if item.is_resource() else item.scope)
            labels.append(f'{url}\t{info}')
        path = Path(matrix_file)
        path.with_name(path.stem + '_nodes.txt').write_text('\n'.join(labels), encoding='utf-8')

    net = Network(width=f'{args.width}px', height=f'{args.height}px', directed=True)
    net.from_nx(graph)
    if args.show_buttons:
        net.show_buttons()
    elif args.options is not None:
        net.set_options(args.options)

    aliases = {}
    if args.interactive_controls:
        for item in result.nodes.values():
            if item.alias_of is not None:
                aliases.setdefault(item.alias_of, []).append(item.id)

    for node in net.nodes:
        item = result.nodes[node['id']]
        error = item.error()
        node.update(size=15, label='', color=(
            EXTERNAL_COLOR if item.scope == 'external' else
            RESOURCE_COLOR if item.is_resource() else INTERNAL_COLOR
        ))
        if error is not None and (not args.only_404 or error == 404):
            node['color'] = ERROR_COLOR
        node['title'] = (f'{html.escape(str(error))} Error: ' if error is not None else '') + url_link(item.id)
        if args.interactive_controls:
            info = exploration_info(item, sorted(aliases.get(item.id, [])), result.generated_at)
            node['crawl'] = info
            node['title'] = url_link(item.id) + '<br>' + html.escape(info['detail']).replace('\n', '<br>')
            if info['state'] == 'unchecked':
                node['shape'] = 'triangle'

    prepare_graph_template(net, args.interactive_controls)
    net.save_graph(args.vis_file)


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
