"""HTML graph presentation, drawing options, and exploration controls."""

import html
import json
import os
import tempfile
import urllib.parse
from pathlib import Path

from jinja2 import ChoiceLoader, DictLoader
from pyvis.network import Network

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
    template = template.replace(
        'function drawGraph() {',
        'var graphData;\n'
        'function drawGraph() {\n'
        'graphData = JSON.parse(document.getElementById("graph-data").textContent);',
    )
    template = template.replace('{{nodes|tojson}}', 'structuredClone(graphData.nodes)')
    template = template.replace('{{edges|tojson}}', 'graphData.edges')
    template = template.replace('{{options|safe}}', 'structuredClone(graphData.options)')
    template = template.replace(
        'drawGraph();',
        'document.addEventListener("DOMContentLoaded", () => {\n'
        'drawGraph();\n'
        '});',
    )
    template = template.replace('</body>', '''
<script id="graph-data" type="application/json">
{"nodes": {{nodes|tojson}}, "edges": {{edges|tojson}}, "options": {{options|script_safe|safe}}}
</script>
</body>''')
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
    net = Network(width=f'{args.width}px', height=f'{args.height}px', directed=True,
                  cdn_resources='remote')
    net.options.edges.smooth.enabled = False
    net.add_nodes(result.graph_nodes())
    for source, target in sorted(result.edges):
        net.add_edge(source, target, width=1)
    if args.show_buttons:
        net.show_buttons()
    elif args.options is not None:
        options = json.loads(args.options)
        options.setdefault('edges', {}).setdefault('smooth', False)
        net.set_options(json.dumps(options))

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
    if not args.prelayout:
        net.save_graph(args.vis_file)
        return

    from prelayout import compute_positions

    positions = compute_positions(net.generate_html(), result.graph_nodes())
    for node in net.nodes:
        node.update(positions[node['id']])
    options = json.loads(net.get_network_data()[-1])
    physics = options.setdefault('physics', {})
    stabilization = physics.get('stabilization', {})
    physics['stabilization'] = {
        **(stabilization if isinstance(stabilization, dict) else {}), 'enabled': False,
    }
    options.setdefault('layout', {}).update(improvedLayout=False, hierarchical=False)
    net.options = options
    document = net.generate_html()
    destination = Path(args.vis_file)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                         suffix='.html', delete=False) as output:
            temporary = Path(output.name)
            output.write(document)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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
