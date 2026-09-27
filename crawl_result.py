"""Crawl model, JSON v1 validation/storage, and console summaries."""

import json
import os
import tempfile
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit


def is_internal(url, site_url):
    target = urlsplit(url)
    root = urlsplit(site_url)

    def origin(parts):
        port = parts.port if parts.port is not None else (443 if parts.scheme == 'https' else 80)
        return parts.scheme, parts.hostname, port

    path = root.path.rstrip('/')
    return origin(target) == origin(root) and (
        target.path == path or target.path.startswith(path + '/')
    )


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


@dataclass
class FetchError:
    kind: str
    message: str


@dataclass
class Observation:
    method: str
    http_status: Optional[int] = None
    fetch_error: Optional[FetchError] = None
    redirect_to: Optional[str] = None
    type: str = 'unknown'
    discovered: bool = False


@dataclass
class CrawlNode:
    id: str
    scope: str
    alias_of: Optional[str] = None
    observations: list[Observation] = field(default_factory=list)

    @property
    def latest(self):
        return self.observations[-1] if self.observations else None

    def error(self):
        latest = self.latest
        if latest is not None:
            if latest.fetch_error is not None:
                return latest.fetch_error.message
            if latest.http_status is not None and latest.http_status >= 400:
                return latest.http_status
        return None

    def is_resource(self):
        return self.latest is not None and self.latest.type == 'resource'

    def to_dict(self):
        latest = self.latest
        return {
            'id': self.id,
            'scope': self.scope,
            'alias_of': self.alias_of,
            'type': latest.type if latest else 'unknown',
            'check_state': 'checked' if latest else 'unchecked',
            'http_status': latest.http_status if latest else None,
            'fetch_error': asdict(latest.fetch_error) if latest and latest.fetch_error else None,
            'observations': [asdict(observation) for observation in self.observations],
        }


def discovery_summary(nodes):
    reasons = []
    for node in nodes:
        if node['scope'] != 'internal' or node['alias_of'] is not None:
            continue
        if any(observation['discovered'] for observation in node['observations']):
            continue
        status = node['http_status']
        if node['fetch_error'] is not None:
            reason = 'fetch_error'
        elif status in (404, 410):
            continue
        elif status is not None and status >= 400:
            reason = 'http_error'
        elif status is not None and 200 <= status < 300 and node['type'] == 'resource':
            continue
        elif node['type'] == 'unknown' and node['check_state'] == 'checked':
            reason = 'unknown_content_type'
        else:
            reason = 'not_discovered'
        reasons.append({'url': node['id'], 'reason': reason})
    return {'complete': not reasons, 'reasons': reasons}


@dataclass
class CrawlResult:
    requested_root_url: str
    resolved_root_url: str
    settings: dict
    nodes: dict[str, CrawlNode] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    generated_at: str = field(default_factory=utc_now)

    def node(self, url):
        if url not in self.nodes:
            self.nodes[url] = CrawlNode(
                url, 'internal' if is_internal(url, self.resolved_root_url) else 'external'
            )
        return self.nodes[url]

    def graph_nodes(self):
        endpoints = {url for edge in self.edges for url in edge}
        redirect_targets = {
            observation.redirect_to for node in self.nodes.values()
            for observation in node.observations if observation.redirect_to is not None
        }
        # A redirect limit can record a destination that was never requested or linked.
        return sorted(url for url, node in self.nodes.items()
                      if url == self.resolved_root_url or (
                          node.alias_of is None and (
                              node.observations or url in endpoints or url not in redirect_targets
                          )))

    def to_dict(self):
        nodes = [self.nodes[url].to_dict() for url in sorted(self.nodes)]
        checked = sum(node['check_state'] == 'checked' for node in nodes)
        return {
            'schema_version': 1,
            'requested_root_url': self.requested_root_url,
            'resolved_root_url': self.resolved_root_url,
            'generated_at': self.generated_at,
            'settings': dict(self.settings),
            'nodes': nodes,
            'edges': [{'source': source, 'target': target} for source, target in sorted(self.edges)],
            'discovery': discovery_summary(nodes),
            'checking': {'checked': checked, 'unchecked': len(nodes) - checked},
        }


def from_dict(data):
    """Load validated v1 data without inferring or discarding observations."""
    validate_crawl_data(data)
    result = CrawlResult(data['requested_root_url'], data['resolved_root_url'],
                         dict(data['settings']), generated_at=data['generated_at'])
    for item in data['nodes']:
        observations = []
        for saved in item['observations']:
            values = dict(saved)
            if values['fetch_error'] is not None:
                values['fetch_error'] = FetchError(**values['fetch_error'])
            observations.append(Observation(**values))
        result.nodes[item['id']] = CrawlNode(item['id'], item['scope'],
                                            item['alias_of'], observations)
    result.edges = {(edge['source'], edge['target']) for edge in data['edges']}
    return result


def load_data(filename):
    """Read and validate a JSON v1 crawl."""
    with Path(filename).open(encoding='utf-8') as source:
        return from_dict(json.load(source))


def validate_crawl_data(data):
    """Reject malformed or internally inconsistent v1 data with a useful path."""
    def require(condition, path):
        if not condition:
            raise ValueError(f'Invalid crawl data: {path}')

    def fields(value, names, path):
        require(type(value) is dict and set(value) == set(names.split()), path)

    def url(value, path):
        require(type(value) is str, path)
        try:
            parts = urlsplit(value)
            port = parts.port
            valid = (parts.scheme in ('http', 'https') and parts.hostname and not parts.fragment
                     and (port is None or 0 <= port <= 65535))
        except ValueError:
            valid = False
        require(valid, path)

    def status(value, path):
        require(value is None or (type(value) is int and 100 <= value <= 599), path)

    def error(value, path):
        if value is not None:
            fields(value, 'kind message', path)
            require(value['kind'] in ('timeout', 'connection', 'request', 'redirect'), path + '.kind')
            require(type(value['message']) is str and bool(value['message']), path + '.message')

    fields(data, 'schema_version requested_root_url resolved_root_url generated_at settings '
           'nodes edges discovery checking', 'fields')
    require(type(data['schema_version']) is int and data['schema_version'] == 1, 'schema_version')
    for key in ('requested_root_url', 'resolved_root_url'):
        url(data[key], key)
    timestamp = data['generated_at']
    require(type(timestamp) is str and timestamp.endswith('Z'), 'generated_at (UTC)')
    try:
        parsed = datetime.fromisoformat(timestamp[:-1] + '+00:00')
    except ValueError:
        raise ValueError('Invalid crawl data: generated_at') from None
    require('T' in timestamp and parsed.tzinfo is not None
            and parsed.utcoffset() == timezone.utc.utcoffset(parsed), 'generated_at (UTC)')
    fields(data['settings'], 'visit_external keep_queries workers', 'settings')
    for key in ('visit_external', 'keep_queries'):
        require(type(data['settings'][key]) is bool, 'settings.' + key)
    require(type(data['settings']['workers']) is int and data['settings']['workers'] >= 1,
            'settings.workers')
    require(type(data['nodes']) is list, 'nodes')
    nodes = {}
    for index, node in enumerate(data['nodes']):
        path = f'nodes[{index}]'
        fields(node, 'id scope alias_of type check_state http_status fetch_error observations', path)
        url(node['id'], path + '.id')
        require(node['id'] not in nodes, path + '.id (duplicate)')
        nodes[node['id']] = node
        status(node['http_status'], path + '.http_status')
        if node['alias_of'] is not None:
            url(node['alias_of'], path + '.alias_of')
        require(type(node['observations']) is list, path + '.observations')
        for observation in node['observations']:
            fields(observation, 'method http_status fetch_error redirect_to type discovered',
                   path + '.observations')
            require(observation['method'] in ('GET', 'HEAD'), path + '.method')
            status(observation['http_status'], path + '.observation.http_status')
            error(observation['fetch_error'], path + '.observation.fetch_error')
            require(observation['type'] in ('page', 'resource', 'unknown'), path + '.observation.type')
            require(type(observation['discovered']) is bool, path + '.discovered')
            require(observation['http_status'] is not None or observation['fetch_error'] is not None,
                    path + '.observation (no outcome)')
            if observation['redirect_to'] is not None:
                url(observation['redirect_to'], path + '.redirect_to')
                require(observation['http_status'] in (301, 302, 303, 307, 308),
                        path + '.redirect status')
            if observation['discovered']:
                require(node['scope'] == 'internal'
                        and observation['method'] == 'GET' and observation['type'] == 'page'
                        and observation['http_status'] is not None
                        and 200 <= observation['http_status'] < 300
                        and observation['fetch_error'] is None
                        and observation['redirect_to'] is None, path + '.discovered')
        latest = node['observations'][-1] if node['observations'] else {
            'http_status': None, 'fetch_error': None, 'type': 'unknown',
        }
        for key in ('http_status', 'fetch_error', 'type'):
            require(node[key] == latest[key], path + '.' + key + ' (summary)')
        require(node['check_state'] == ('checked' if node['observations'] else 'unchecked'),
                path + '.check_state (summary)')

    for node in nodes.values():
        internal = is_internal(node['id'], data['resolved_root_url'])
        require(node['scope'] == ('internal' if internal else 'external'), node['id'] + '.scope')
        alias = node['alias_of']
        if alias is not None:
            require(alias in nodes and alias != node['id'] and nodes[alias]['alias_of'] is None,
                    node['id'] + '.alias_of')
            require(any(item['redirect_to'] is not None for item in node['observations']),
                    node['id'] + '.alias_of (no redirect)')
        for observation in node['observations']:
            target = observation['redirect_to']
            require(target is None or target in nodes, node['id'] + '.redirect_to reference')
    require(data['requested_root_url'] in nodes, 'requested_root_url reference')
    require(data['resolved_root_url'] in nodes, 'resolved_root_url reference')
    require(any(item['discovered'] for item in nodes[data['resolved_root_url']]['observations']),
            'resolved_root_url (HTML discovery required)')
    require(type(data['edges']) is list, 'edges')
    edges = set()
    for edge in data['edges']:
        fields(edge, 'source target', 'edge')
        for key in ('source', 'target'):
            url(edge[key], 'edge.' + key)
            require(edge[key] in nodes and nodes[edge[key]]['alias_of'] is None,
                    'edge.' + key + ' reference')
        pair = edge['source'], edge['target']
        require(pair not in edges, 'duplicate edge')
        edges.add(pair)
    fields(data['checking'], 'checked unchecked', 'checking')
    checked = sum(node['check_state'] == 'checked' for node in nodes.values())
    for key, expected in (('checked', checked), ('unchecked', len(nodes) - checked)):
        require(type(data['checking'][key]) is int and data['checking'][key] == expected,
                'checking.' + key)
    fields(data['discovery'], 'complete reasons', 'discovery')
    require(type(data['discovery']['complete']) is bool, 'discovery.complete')
    require(data['discovery'] == discovery_summary(data['nodes']), 'discovery (summary)')


def write_json(result, filename):
    """Validate before touching the destination; replace only after a full write."""
    data = result.to_dict()
    validate_crawl_data(data)
    destination = Path(filename)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                         prefix=f'.{destination.name}.', suffix='.tmp',
                                         delete=False) as output:
            temporary = output.name
            json.dump(data, output, indent=2, ensure_ascii=True, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def summarize(result):
    nodes = [result.nodes[url] for url in sorted(result.nodes)
             if result.nodes[url].alias_of is None]
    aliases = len(result.nodes) - len(nodes)
    graph_nodes = set(result.graph_nodes())
    observation_only = sum(node.id not in graph_nodes for node in nodes)
    lines = [f'Graph: {len(graph_nodes)} canonical nodes, {len(result.edges)} directed edges, '
             f'{aliases} aliases.']
    if observation_only:
        lines.append(f'JSON only: {observation_only} unfollowed redirect destinations '
                     '(not linked or checked).')
    discovery = discovery_summary(node.to_dict() for node in nodes)
    reasons = Counter(item['reason'] for item in discovery['reasons'])
    detail = ', '.join(f'{reason}={count}' for reason, count in sorted(reasons.items()))
    lines.append('Discovery: ' + ('complete.' if discovery['complete'] else f'incomplete ({detail}).'))
    checked = [node.latest for node in nodes if node.latest]
    categories = Counter(obs.http_status // 100 for obs in checked if obs.http_status is not None)
    failures = sum(obs.fetch_error is not None for obs in checked)
    lines.append(f'Checking (canonical): {len(checked)} checked, {len(nodes) - len(checked)} unchecked; '
                 f'{failures} fetch failures.')
    lines.append('Latest HTTP responses: ' + ', '.join(
        f'{category}xx={categories[category]}' for category in range(1, 6)
    ) + '. HTTP responses and fetch failures can overlap.')
    broken = [node for node in nodes if node.error() is not None]
    lines.append(f'Broken targets: {len(broken)}.')
    referrers = {}
    for source, target in sorted(result.edges):
        referrers.setdefault(target, []).append(source)
    for node in broken:
        lines.append(f'  {node.id}: {node.error()}')
        lines.append('    Referred by: ' + (', '.join(referrers.get(node.id, [])) or '(none recorded)'))
    return '\n'.join(lines)
