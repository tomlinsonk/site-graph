import copy
import json
import pickle
import runpy
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import requests

from crawl_result import validate_crawl_data, write_json
from site_graph import crawl, crawl_site, fetch, is_internal, resolve_url, visualize


def response(status=200, body='', headers=None):
    result = MagicMock()
    result.status_code = status
    result.headers = {'Content-Type': 'text/html', **(headers or {})}
    result.is_redirect = status in (301, 302, 303, 307, 308) and 'Location' in result.headers
    result.text = body
    result.__enter__.return_value = result
    return result


class CrawlTests(unittest.TestCase):
    def test_url_identity_and_scope(self):
        root = 'https://example.org/~kt/'
        for url, expected in [
            (root, True), (root[:-1], True), (root + 'page', True),
            ('https://example.org/~kt-other/', False),
            ('https://example.org:443/~kt/page', True),
            ('https://example.org:8443/~kt/page', False),
            ('http://example.org/~kt/', False),
            ('https://example.org.evil.test/~kt/', False),
        ]:
            with self.subTest(url=url):
                self.assertEqual(is_internal(url, root), expected)
        self.assertEqual(resolve_url('page?q=1#top', root, root, False), root + 'page')
        self.assertEqual(resolve_url('page?q=1#top', root, root, True), root + 'page?q=1')
        self.assertEqual(resolve_url('/outside?q=1#top', root, root, False),
                         'https://example.org/outside?q=1')
        self.assertEqual(resolve_url('HTTPS://EXAMPLE.ORG:443/#top'), 'https://example.org/')
        self.assertNotEqual(resolve_url('page', root), resolve_url('page/', root))
        for href in ('mailto:a@example.org', 'javascript:void(0)', 'data:text/plain,hi', 'ftp://example.org'):
            self.assertIsNone(resolve_url(href, root))

    @patch('site_graph.requests.Session')
    def test_crawl_redirects_base_aliases_and_scope(self, session_class):
        origin = 'https://example.org'
        root = origin + '/~kt/'
        outside = response(body='must not be parsed')
        type(outside).text = PropertyMock(side_effect=AssertionError('read external HTML'))
        routes = {
            ('GET', origin + '/start'): response(301, headers={'Location': '/~kt/'}),
            ('GET', origin + '/consent'): response(302, headers={'Location': '/~kt/'}),
            ('GET', root): response(body='''<base href="sub/">
                <a href="alias#one"></a><a href="alias#two"></a>
                <a href="page"></a><a href="page/"></a>
                <a href="bad-alias"></a><a href="missing"></a>
                <a href="asset"></a><a href="exit"></a>
                <a href="/~kt-other/"></a><a href="../#root"></a>'''),
            ('GET', root + 'sub/alias'): response(302, headers={'Location': 'destination/'}),
            ('GET', root + 'sub/destination/'): response(body='<a href="child?q=1#top"></a>'),
            ('GET', root + 'sub/destination/child'): response(),
            ('GET', root + 'sub/page'): response(body='<a href="bad-alias"></a>'),
            ('GET', root + 'sub/page/'): response(),
            ('GET', root + 'sub/bad-alias'): response(308, headers={'Location': 'missing'}),
            ('GET', root + 'sub/missing'): response(404),
            ('GET', root + 'sub/asset'): response(headers={'Content-Type': 'application/pdf'}),
            ('GET', root + 'sub/exit'): response(302, headers={'Location': '/outside'}),
            ('HEAD', origin + '/outside'): outside,
        }
        session = session_class.return_value.__enter__.return_value
        root_responses = iter([response(302, headers={'Location': '/consent'}),
                               routes[('GET', root)]])

        def request(method, url, **kwargs):
            return next(root_responses) if url == root else routes[(method, url)]

        session.request.side_effect = request
        result = crawl_site(origin + '/start', False, False)
        validate_crawl_data(result.to_dict())
        self.assertEqual([item.http_status for item in result.nodes[root].observations], [302, 200])
        edges, errors, resources, resolved_root = result.legacy_data()
        self.assertEqual(resolved_root, root)
        self.assertIn((root, root), edges)
        self.assertIn((root, root + 'sub/destination/'), edges)
        self.assertIn((root + 'sub/destination/', root + 'sub/destination/child'), edges)
        self.assertIn((root, root + 'sub/page'), edges)
        self.assertIn((root, root + 'sub/page/'), edges)
        self.assertIn((root + 'sub/page', root + 'sub/missing'), edges)
        self.assertIn((root, origin + '/~kt-other/'), edges)
        self.assertIn((root, origin + '/outside'), edges)
        self.assertEqual(errors, {root + 'sub/missing': 404})
        self.assertEqual(resources, {root + 'sub/asset'})
        counts = Counter(call.args for call in session.request.call_args_list)
        self.assertEqual(counts.pop(('GET', root)), 2)
        self.assertTrue(all(count == 1 for count in counts.values()))
        self.assertEqual(set(counts) | {('GET', root)}, set(routes))

    @patch('site_graph.time.sleep')
    def test_http_fallback_retries_and_errors(self, sleep):
        for status in (405, 501):
            with self.subTest(head_status=status):
                session = MagicMock()
                fallback = response()
                type(fallback).text = PropertyMock(side_effect=AssertionError('read availability body'))
                session.request.side_effect = [response(status), fallback]
                result = fetch(session, 'https://example.org/')
                self.assertEqual(result.status, 200)
                self.assertIsNone(result.text)
                self.assertEqual([call.args[0] for call in session.request.call_args_list], ['HEAD', 'GET'])
                self.assertTrue(all(call.kwargs['stream'] for call in session.request.call_args_list))
                fallback.__exit__.assert_called_once()
        session = MagicMock()
        session.request.side_effect = [response(503, headers={'Retry-After': '2'}), response()]
        self.assertEqual(fetch(session, 'https://example.org/').status, 200)
        sleep.assert_called_with(2)
        session.request.side_effect = None
        session.request.return_value = response(429, headers={'Retry-After': '120'})
        session.request.reset_mock()
        self.assertEqual(fetch(session, 'https://example.org/').status, 429)
        session.request.assert_called_once()
        for error in (requests.exceptions.Timeout('timeout'), requests.exceptions.ConnectionError('offline')):
            session.request.side_effect = error
            session.request.reset_mock()
            result = fetch(session, 'https://example.org/')
            self.assertIn(type(error).__name__, result.error)
            self.assertIsNone(result.status)
            self.assertEqual(session.request.call_count, 3)
        session.request.side_effect = None
        session.request.return_value = response(301, headers={'Location': '/'})
        session.request.reset_mock()
        result = fetch(session, 'https://example.org/')
        self.assertIn('Redirect limit', result.error)
        self.assertEqual(result.observations[-1][1].redirect_to, 'https://example.org/')
        self.assertEqual(session.request.call_count, 11)
        session.request.side_effect = [response(405)] + [requests.exceptions.Timeout('fallback')] * 3
        result = fetch(session, 'https://example.org/')
        self.assertIsNone(result.status)
        self.assertEqual([item.http_status for _, item in result.observations], [405, None])
        self.assertEqual(result.observations[-1][1].fetch_error.kind, 'timeout')
        session.request.side_effect = [response(301)]
        result = fetch(session, 'https://example.org/')
        self.assertEqual(result.observations[0][1].fetch_error.kind, 'redirect')
        self.assertEqual(result.observations[0][1].http_status, 301)

    @patch('site_graph.requests.Session')
    def test_parallel_external_checks_preserve_results(self, session_class):
        root = 'https://example.org/'
        barrier = Barrier(2)

        def request(method, url, **kwargs):
            if url == root:
                self.assertEqual(method, 'GET')
                return response(body='<a href="https://a.test/"></a><a href="https://b.test/"></a>')
            self.assertEqual(method, 'HEAD')
            barrier.wait(timeout=5)
            return response(404)

        def new_session():
            session = MagicMock()
            session.__enter__.return_value = session
            session.request.side_effect = request
            return session

        session_class.side_effect = new_session
        edges, errors, resources, resolved_root = crawl(root, True, False, workers=2)
        self.assertEqual(edges, {(root, 'https://a.test/'), (root, 'https://b.test/')})
        self.assertEqual(errors, {'https://a.test/': 404, 'https://b.test/': 404})
        self.assertEqual(resources, set())
        self.assertEqual(resolved_root, root)
        self.assertEqual(session_class.call_count, 3)

    @patch('site_graph.requests.Session')
    def test_empty_and_unusable_roots(self, session_class):
        root = 'https://example.org/'
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response()
        self.assertEqual(crawl(root, False, False), (set(), {}, set(), root))
        args = SimpleNamespace(site_url=root, save_txt=None, save_npz=None,
                               width=1000, height=800, show_buttons=False, options=None,
                               only_404=False, vis_file='unused.html')
        with patch('site_graph.Network') as network:
            visualize(set(), {}, set(), args)
            graph = network.return_value.from_nx.call_args.args[0]
            self.assertEqual(set(graph.nodes), {root})
        for unusable in (response(404), response(headers={'Content-Type': 'application/pdf'})):
            session.request.return_value = unusable
            with self.assertRaisesRegex(ValueError, 'Cannot crawl root'):
                crawl(root, False, False)


class JsonTests(unittest.TestCase):
    root = 'https://example.org/'
    timestamp = '2026-09-26T07:00:00Z'

    @patch('site_graph.requests.Session')
    def test_deterministic_contract_and_explicit_nodes(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response()
        isolated = crawl_site(self.root, False, False, generated_at=self.timestamp)
        self.assertEqual(isolated.graph_nodes(), [self.root])
        self.assertEqual(isolated.to_dict()['edges'], [])
        validate_crawl_data(isolated.to_dict())

        def run(links):
            def request(method, url, **kwargs):
                if url == self.root + 'start':
                    return response(301, headers={'Location': '/'})
                return response(body=links if url == self.root else '')
            session.request.side_effect = request
            return crawl_site(self.root + 'start', False, False, generated_at=self.timestamp)

        links = ['<a href="z"></a>', '<a href="a"></a>', '<a href="https://outside.test/"></a>']
        first = run(''.join(links))
        second = run(''.join(reversed(links)))
        data = first.to_dict()
        validate_crawl_data(data)
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['requested_root_url'], self.root + 'start')
        self.assertEqual(data['resolved_root_url'], self.root)
        self.assertEqual(data['generated_at'], self.timestamp)
        self.assertEqual(data['settings'], {'visit_external': False, 'keep_queries': False, 'workers': 4})
        self.assertEqual(data['discovery'], {'complete': True, 'reasons': []})
        self.assertEqual(data['checking'], {'checked': 4, 'unchecked': 1})
        unchecked = next(node for node in data['nodes'] if node['id'] == 'https://outside.test/')
        self.assertEqual(unchecked, {
            'id': 'https://outside.test/', 'scope': 'external', 'alias_of': None,
            'type': 'unknown', 'check_state': 'unchecked', 'http_status': None,
            'fetch_error': None, 'observations': [],
        })
        self.assertEqual([node['id'] for node in data['nodes']], sorted(first.nodes))
        self.assertEqual([(edge['source'], edge['target']) for edge in data['edges']], sorted(first.edges))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crawl.json'
            write_json(first, path)
            original = path.read_bytes()
            write_json(second, path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(original), data)
        self.assertNotIn('"color"', original.decode())
        self.assertNotIn('"title"', original.decode())
        self.assertNotIn('"tooltip"', original.decode())

    @patch('site_graph.time.sleep')
    @patch('site_graph.requests.Session')
    def test_status_errors_alias_history_and_discovery(self, session_class, sleep):
        session = session_class.return_value.__enter__.return_value
        body_error = response()
        type(body_error).text = PropertyMock(side_effect=requests.exceptions.Timeout('body timeout'))
        routes = {
            self.root: response(body=''.join(f'<a href="{name}"></a>' for name in
                                            ('alias', 'alias2', 'gone', 'timeout', 'offline',
                                             'busy', 'asset', 'body', 'unknown'))),
            self.root + 'alias': response(302, headers={'Location': '/missing'}),
            self.root + 'alias2': response(308, headers={'Location': '/missing'}),
            self.root + 'gone': response(404),
            self.root + 'timeout': requests.exceptions.Timeout('timeout'),
            self.root + 'offline': requests.exceptions.ConnectionError('offline'),
            self.root + 'busy': response(503, headers={'Retry-After': '120'}),
            self.root + 'asset': response(headers={'Content-Type': 'application/pdf'}),
            self.root + 'body': body_error,
            self.root + 'unknown': response(headers={'Content-Type': ''}),
        }
        missing = iter([response(404), response(410)])

        def request(method, url, **kwargs):
            if url == self.root + 'missing':
                return next(missing)
            result = routes[url]
            if isinstance(result, Exception):
                raise result
            return result

        session.request.side_effect = request
        result = crawl_site(self.root, False, False)
        data = result.to_dict()
        validate_crawl_data(data)
        nodes = {node['id']: node for node in data['nodes']}
        self.assertEqual(nodes[self.root + 'missing']['http_status'], 410)
        self.assertEqual([item['http_status'] for item in nodes[self.root + 'missing']['observations']],
                         [404, 410])
        for name, status in (('alias', 302), ('alias2', 308)):
            self.assertEqual(nodes[self.root + name]['http_status'], status)
            self.assertEqual(nodes[self.root + name]['alias_of'], self.root + 'missing')
            self.assertEqual(nodes[self.root + name]['observations'][0]['redirect_to'],
                             self.root + 'missing')
        self.assertEqual({(edge['source'], edge['target']) for edge in data['edges']},
                         result.legacy_data()[0])
        for name, kind in (('timeout', 'timeout'), ('offline', 'connection'), ('body', 'timeout')):
            self.assertEqual(nodes[self.root + name]['fetch_error']['kind'], kind)
            self.assertEqual(nodes[self.root + name]['http_status'], 200 if name == 'body' else None)
        for name, status in (('gone', 404), ('busy', 503)):
            self.assertEqual(nodes[self.root + name]['http_status'], status)
            self.assertIsNone(nodes[self.root + name]['fetch_error'])
        self.assertEqual(nodes[self.root + 'asset']['type'], 'resource')
        self.assertFalse(data['discovery']['complete'])
        self.assertEqual(data['discovery']['reasons'], [
            {'url': self.root + 'body', 'reason': 'fetch_error'},
            {'url': self.root + 'busy', 'reason': 'http_error'},
            {'url': self.root + 'offline', 'reason': 'fetch_error'},
            {'url': self.root + 'timeout', 'reason': 'fetch_error'},
            {'url': self.root + 'unknown', 'reason': 'unknown_content_type'},
        ])
        routes[self.root] = response(body='<a href="gone"></a><a href="https://unchecked.test/"></a>')
        data = crawl_site(self.root, False, False).to_dict()
        validate_crawl_data(data)
        self.assertTrue(data['discovery']['complete'])
        self.assertEqual(data['checking'], {'checked': 2, 'unchecked': 1})

    @patch('site_graph.requests.Session')
    def test_external_redirect_check_is_not_internal_discovery(self, session_class):
        session = session_class.return_value.__enter__.return_value
        session.request.side_effect = [
            response(body='<a href="https://outside.test/"></a>'),
            response(301, headers={'Location': self.root + 'new'}),
            response(405), response(),
        ]
        data = crawl_site(self.root, True, False, workers=1).to_dict()
        validate_crawl_data(data)
        node = next(node for node in data['nodes'] if node['id'] == self.root + 'new')
        self.assertEqual(node['http_status'], 200)
        self.assertEqual([item['http_status'] for item in node['observations']], [405, 200])
        self.assertEqual([item['method'] for item in node['observations']], ['HEAD', 'GET'])
        self.assertFalse(any(item['discovered'] for item in node['observations']))
        self.assertEqual(data['checking'], {'checked': 3, 'unchecked': 0})
        self.assertEqual(data['discovery'], {
            'complete': False, 'reasons': [{'url': self.root + 'new', 'reason': 'not_discovered'}],
        })

    @patch('site_graph.requests.Session')
    def test_validation_and_atomic_replacement(self, session_class):
        session_class.return_value.__enter__.return_value.request.return_value = response()
        result = crawl_site(self.root, False, False)
        data = result.to_dict()
        mutations = [
            lambda value: value.pop('schema_version'),
            lambda value: value.update(schema_version=True),
            lambda value: value.update(generated_at='2026-09-26Z'),
            lambda value: value['settings'].update(workers='4'),
            lambda value: value.update(resolved_root_url='https://absent.test/'),
            lambda value: value['nodes'].append(copy.deepcopy(value['nodes'][0])),
            lambda value: value['nodes'][0].update(id='mailto:test@example.org'),
            lambda value: value['nodes'][0].update(scope='external'),
            lambda value: value['nodes'][0].update(alias_of=self.root),
            lambda value: value['nodes'][0].update(http_status='200'),
            lambda value: value['nodes'][0].update(check_state='unchecked'),
            lambda value: value['nodes'][0].update(fetch_error={'message': 'missing kind'}),
            lambda value: value['nodes'][0].update(color='red'),
            lambda value: value['nodes'][0]['observations'][0].update(redirect_to='https://absent.test/'),
            lambda value: value['nodes'][0]['observations'][0].update(method='HEAD'),
            lambda value: value['edges'].append({'source': self.root, 'target': 'https://absent.test/'}),
            lambda value: value['checking'].update(checked=False),
            lambda value: value['discovery'].update(complete=False),
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crawl.json'
            path.write_text('previous export')
            for index, mutate in enumerate(mutations):
                with self.subTest(mutation=index):
                    invalid = copy.deepcopy(data)
                    mutate(invalid)
                    with patch.object(result, 'to_dict', return_value=invalid):
                        with self.assertRaises(ValueError):
                            write_json(result, path)
                    self.assertEqual(path.read_text(), 'previous export')
                    self.assertEqual(list(Path(directory).iterdir()), [path])
            for operation in ('json.dump', 'os.replace'):
                with self.subTest(failed_operation=operation):
                    with patch('crawl_result.' + operation, side_effect=OSError('disk failure')):
                        with self.assertRaisesRegex(OSError, 'disk failure'):
                            write_json(result, path)
                    self.assertEqual(path.read_text(), 'previous export')
                    self.assertEqual(list(Path(directory).iterdir()), [path])
            write_json(result, path)
            self.assertEqual(json.loads(path.read_text()), data)
            self.assertEqual(list(Path(directory).iterdir()), [path])

    @patch('pyvis.network.Network')
    @patch('site_graph.requests.Session')
    def test_cli_json_pickle_and_presentation_compatibility(self, session_class, network):
        session = session_class.return_value.__enter__.return_value
        session.request.side_effect = [
            response(body='<a href="gone"></a><a href="denied"></a>'), response(404), response(403),
        ]
        script = str(Path(__file__).with_name('site_graph.py'))
        with tempfile.TemporaryDirectory() as directory:
            data_file = str(Path(directory) / 'crawl.pickle')
            json_file = str(Path(directory) / 'crawl.json')
            vis_file = str(Path(directory) / 'site.html')
            options = Path(directory) / 'options.txt'
            options.write_text('{"physics": {"enabled": false}}')
            network.return_value.nodes = [{'id': self.root + name} for name in ('', 'gone', 'denied')]
            with patch('sys.argv', [script, self.root, '--only-404', '--options', str(options),
                                    '--json-file', json_file, '--data-file', data_file,
                                    '--vis-file', vis_file]):
                runpy.run_path(script, run_name='__main__')
            data = json.loads(Path(json_file).read_text())
            validate_crawl_data(data)
            self.assertEqual([node['http_status'] for node in data['nodes']], [200, 403, 404])
            self.assertEqual([node['color'] for node in network.return_value.nodes],
                             ['#0072BB', '#FF0800', '#0072BB'])
            network.return_value.set_options.assert_called_once_with(options.read_text())
            network.return_value.save_graph.assert_called_once_with(vis_file)
            with open(data_file, 'rb') as saved:
                legacy = pickle.load(saved)
            self.assertIs(type(legacy), tuple)
            self.assertEqual(legacy[1], {self.root + 'gone': 404, self.root + 'denied': 403})
            session.request.reset_mock()
            with patch('sys.argv', [script, '--from-data-file', data_file, '--vis-file', vis_file]):
                runpy.run_path(script, run_name='__main__')
            session.request.assert_not_called()
            with patch('sys.argv', [script, '--from-data-file', data_file, '--json-file', json_file]):
                with self.assertRaises(SystemExit) as error:
                    runpy.run_path(script, run_name='__main__')
            self.assertEqual(error.exception.code, 2)
            for extra, filename in (([], 'crawl.pickle'), ([], 'site.html'),
                                    (['--save-txt', 'matrix.txt'], 'matrix.txt'),
                                    (['--save-npz', 'matrix.npz'], 'matrix_nodes.txt'),
                                    (['--save-npz', 'matrix'], 'matrix.npz'),
                                    (['--options', 'options.txt'], 'options.txt')):
                with self.subTest(output_collision=filename):
                    with patch('sys.argv', [script, self.root, '--json-file', filename] + extra):
                        with self.assertRaises(SystemExit) as error:
                            runpy.run_path(script, run_name='__main__')
                    self.assertEqual(error.exception.code, 2)
                    session.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
