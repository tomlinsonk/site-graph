"""Version 1 crawl data, independent of visualization and pickle storage."""

import json
import os
import pickle
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union
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
    metadata_known: bool = True
    legacy_error: Optional[Union[int, str]] = None
    legacy_resource: bool = False

    def error(self):
        if not self.metadata_known:
            return self.legacy_error
        latest = self.observations[-1] if self.observations else None
        if latest is not None:
            if latest.fetch_error is not None:
                return latest.fetch_error.message
            if latest.http_status is not None and latest.http_status >= 400:
                return latest.http_status
        return None

    def is_resource(self):
        return self.legacy_resource if not self.metadata_known else (
            bool(self.observations) and self.observations[-1].type == 'resource'
        )

    def to_dict(self):
        latest = self.observations[-1] if self.observations else Observation('GET')
        return {
            'id': self.id,
            'scope': self.scope,
            'alias_of': self.alias_of,
            'type': latest.type,
            'check_state': ('unknown' if not self.metadata_known else
                            'checked' if self.observations else 'unchecked'),
            'http_status': latest.http_status,
            'fetch_error': asdict(latest.fetch_error) if latest.fetch_error else None,
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
    requested_root_url: Optional[str]
    resolved_root_url: str
    settings: dict
    nodes: dict[str, CrawlNode] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    generated_at: Optional[str] = field(default_factory=utc_now)
    is_legacy: bool = False

    def graph_nodes(self):
        return sorted(url for url, node in self.nodes.items() if node.alias_of is None)

    def legacy_data(self):
        errors = {}
        resources = set()
        for url in self.graph_nodes():
            node = self.nodes[url]
            if node.error() is not None:
                errors[url] = node.error()
            elif node.is_resource() or (
                    node.metadata_known and node.observations
                    and node.observations[-1].type == 'unknown'):
                resources.add(url)
        return self.edges, errors, resources, self.resolved_root_url

    def to_dict(self):
        if self.is_legacy:
            raise ValueError('Legacy pickle has unknown crawl metadata; JSON export requires v1 JSON or a new crawl')
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


def from_legacy(data):
    """Adapt a trusted old tuple for rendering, not as evidence of a crawl."""
    if not isinstance(data, tuple) or len(data) != 4:
        raise ValueError('Invalid legacy crawl data: expected (edges, errors, resources, root)')
    edges, errors, resources, root = data
    if not isinstance(edges, (set, list, tuple)) or not isinstance(errors, dict) or not isinstance(
            resources, (set, list, tuple)):
        raise ValueError('Invalid legacy crawl data: edges, errors or resources')
    urls = [root, *errors, *resources]
    for edge in edges:
        if not isinstance(edge, (tuple, list)) or len(edge) != 2:
            raise ValueError('Invalid legacy crawl data: directed edge pair required')
        urls.extend(edge)
    for url in urls:
        try:
            parts = urlsplit(url) if isinstance(url, str) else None
            valid = parts is not None and parts.scheme in ('http', 'https') and parts.hostname
            if valid:
                parts.port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(f'Invalid legacy crawl data: URL {url!r}')
    if any(not (type(error) is str or type(error) is int) for error in errors.values()):
        raise ValueError('Invalid legacy crawl data: error value')
    result = CrawlResult(None, root, {'visit_external': None, 'keep_queries': None, 'workers': None},
                         generated_at=None, is_legacy=True)
    result.edges = {tuple(edge) for edge in edges}
    for url in urls:
        result.nodes[url] = CrawlNode(
            url, 'internal' if is_internal(url, root) else 'external',
            metadata_known=False, legacy_error=errors.get(url), legacy_resource=url in resources,
        )
    return result


def load_data(filename):
    """Read JSON, or an explicitly named trusted .pickle/.pkl legacy file."""
    path = Path(filename)
    if path.suffix.lower() in ('.pickle', '.pkl'):
        with path.open('rb') as source:
            try:
                return from_legacy(pickle.load(source))
            except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, IndexError) as error:
                raise ValueError(f'Invalid legacy crawl data: {error}') from error
    with path.open(encoding='utf-8') as source:
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
        require(node['scope'] in ('internal', 'external'), path + '.scope')
        require(node['type'] in ('page', 'resource', 'unknown'), path + '.type')
        require(node['check_state'] in ('checked', 'unchecked'), path + '.check_state')
        status(node['http_status'], path + '.http_status')
        error(node['fetch_error'], path + '.fetch_error')
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
        require((node['check_state'] == 'checked') == bool(node['observations']),
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
