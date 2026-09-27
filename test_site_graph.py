import copy
import contextlib
import io
import json
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import requests
from bs4 import BeautifulSoup
from pyvis.network import Network

from crawl_result import (
    CrawlNode, CrawlResult, FetchError, Observation, from_dict, is_internal,
    load_data, summarize, validate_crawl_data, write_json,
)
from crawler import crawl_site, fetch, resolve_url
from render import read_options, url_link, visualize
from site_graph import main


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

    @patch('crawler.requests.Session')
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
        edges = result.edges
        self.assertEqual(result.resolved_root_url, root)
        self.assertIn((root, root), edges)
        self.assertIn((root, root + 'sub/destination/'), edges)
        self.assertIn((root + 'sub/destination/', root + 'sub/destination/child'), edges)
        self.assertIn((root, root + 'sub/page'), edges)
        self.assertIn((root, root + 'sub/page/'), edges)
        self.assertIn((root + 'sub/page', root + 'sub/missing'), edges)
        self.assertIn((root, origin + '/~kt-other/'), edges)
        self.assertIn((root, origin + '/outside'), edges)
        self.assertEqual({url: node.error() for url, node in result.nodes.items()
                          if node.error() is not None}, {root + 'sub/missing': 404})
        self.assertEqual({url for url, node in result.nodes.items() if node.is_resource()},
                         {root + 'sub/asset'})
        counts = Counter(call.args for call in session.request.call_args_list)
        self.assertEqual(counts.pop(('GET', root)), 2)
        self.assertTrue(all(count == 1 for count in counts.values()))
        self.assertEqual(set(counts) | {('GET', root)}, set(routes))

    @patch('crawler.time.sleep')
    def test_http_fallback_retries_and_errors(self, sleep):
        for status in (405, 501):
            with self.subTest(head_status=status):
                session = MagicMock()
                fallback = response()
                type(fallback).text = PropertyMock(side_effect=AssertionError('read availability body'))
                session.request.side_effect = [response(status), fallback]
                result = fetch(session, 'https://example.org/')
                self.assertEqual(result.observations[-1][1].http_status, 200)
                self.assertIsNone(result.text)
                self.assertEqual([call.args[0] for call in session.request.call_args_list], ['HEAD', 'GET'])
                self.assertTrue(all(call.kwargs['stream'] for call in session.request.call_args_list))
                fallback.__exit__.assert_called_once()
        session = MagicMock()
        session.request.side_effect = [response(503, headers={'Retry-After': '2'}), response()]
        self.assertEqual(fetch(session, 'https://example.org/').observations[-1][1].http_status, 200)
        sleep.assert_called_with(2)
        session.request.side_effect = None
        session.request.return_value = response(429, headers={'Retry-After': '120'})
        session.request.reset_mock()
        self.assertEqual(fetch(session, 'https://example.org/').observations[-1][1].http_status, 429)
        session.request.assert_called_once()
        for error in (requests.exceptions.Timeout('timeout'), requests.exceptions.ConnectionError('offline')):
            session.request.side_effect = error
            session.request.reset_mock()
            result = fetch(session, 'https://example.org/')
            self.assertIn(type(error).__name__, result.observations[-1][1].fetch_error.message)
            self.assertIsNone(result.observations[-1][1].http_status)
            self.assertEqual(session.request.call_count, 3)
        session.request.side_effect = None
        session.request.return_value = response(301, headers={'Location': '/'})
        session.request.reset_mock()
        result = fetch(session, 'https://example.org/')
        self.assertIn('Redirect limit', result.observations[-1][1].fetch_error.message)
        self.assertEqual(result.observations[-1][1].redirect_to, 'https://example.org/')
        self.assertEqual(session.request.call_count, 11)
        session.request.side_effect = [response(405)] + [requests.exceptions.Timeout('fallback')] * 3
        result = fetch(session, 'https://example.org/')
        self.assertIsNone(result.observations[-1][1].http_status)
        self.assertEqual([item.http_status for _, item in result.observations], [405, None])
        self.assertEqual(result.observations[-1][1].fetch_error.kind, 'timeout')
        session.request.side_effect = [response(301)]
        result = fetch(session, 'https://example.org/')
        self.assertEqual(result.observations[0][1].fetch_error.kind, 'redirect')
        self.assertEqual(result.observations[0][1].http_status, 301)

    @patch('crawler.requests.Session')
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
        result = crawl_site(root, True, False, workers=2)
        self.assertEqual(result.edges, {(root, 'https://a.test/'), (root, 'https://b.test/')})
        self.assertEqual({url: node.error() for url, node in result.nodes.items()
                          if node.error() is not None}, {'https://a.test/': 404, 'https://b.test/': 404})
        self.assertFalse(any(node.is_resource() for node in result.nodes.values()))
        self.assertEqual(result.resolved_root_url, root)
        self.assertEqual(session_class.call_count, 3)

    @patch('crawler.requests.Session')
    def test_empty_and_unusable_roots(self, session_class):
        root = 'https://example.org/'
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response()
        result = crawl_site(root)
        self.assertEqual(result.edges, set())
        self.assertEqual(result.resolved_root_url, root)
        args = SimpleNamespace(site_url=root,
                               width=1000, height=800, show_buttons=False, options=None,
                               only_404=False, interactive_controls=False, prelayout=False, vis_file='unused.html')
        with patch('render.Network') as network:
            visualize(result, args)
            network.return_value.add_nodes.assert_called_once_with([root])
        for unusable in (response(404), response(headers={'Content-Type': 'application/pdf'})):
            session.request.return_value = unusable
            with self.assertRaisesRegex(ValueError, 'Cannot crawl root'):
                crawl_site(root)


class JsonTests(unittest.TestCase):
    root = 'https://example.org/'
    timestamp = '2026-09-26T07:00:00Z'

    @patch('crawler.requests.Session')
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

    @patch('crawler.time.sleep')
    @patch('crawler.requests.Session')
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
                         result.edges)
        summary = summarize(result)
        self.assertIn(self.root + 'missing: 410', summary)
        self.assertIn('Referred by: ' + self.root, summary)
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

    @patch('crawler.requests.Session')
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

    @patch('crawler.requests.Session')
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
            lambda value: value['nodes'][0].update(http_status=200.0),
            lambda value: value['nodes'][0].update(type='invalid'),
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

    @patch('render.Network')
    @patch('crawler.requests.Session')
    def test_cli_json_default_and_presentation(self, session_class, network):
        session = session_class.return_value.__enter__.return_value
        session.request.side_effect = [
            response(body='<a href="gone"></a><a href="denied"></a>'), response(404), response(403),
        ]
        script = str(Path(__file__).with_name('site_graph.py'))
        with tempfile.TemporaryDirectory() as directory:
            json_file = str(Path(directory) / 'crawl.json')
            vis_file = str(Path(directory) / 'site.html')
            options = Path(directory) / 'options.txt'
            options.write_text('{"physics": {"enabled": false}}')
            network.return_value.nodes = [{'id': self.root + name} for name in ('', 'gone', 'denied')]
            with contextlib.chdir(directory), \
                    patch('sys.argv', [script, self.root, '--only-404', '--options', str(options),
                                    '--vis-file', vis_file]):
                runpy.run_path(script, run_name='__main__')
            data = json.loads(Path(json_file).read_text())
            validate_crawl_data(data)
            self.assertEqual([node['http_status'] for node in data['nodes']], [200, 403, 404])
            self.assertEqual([node['color'] for node in network.return_value.nodes],
                             ['#0072BB', '#FF0800', '#0072BB'])
            self.assertEqual(json.loads(network.return_value.set_options.call_args.args[0]),
                             {'physics': {'enabled': False}, 'edges': {'smooth': False}})
            network.return_value.save_graph.assert_called_once_with(vis_file)
            self.assertFalse(list(Path(directory).glob('*.pickle')))
            session.request.reset_mock()
            with patch('sys.argv', [script, '--from-data-file', json_file, '--vis-file', vis_file]):
                runpy.run_path(script, run_name='__main__')
            session.request.assert_not_called()
            for flag in ('--data-file', '--force', '--save-txt', '--save-npz', '--recheck-external'):
                with self.subTest(removed=flag), patch('sys.argv', [script, flag]), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        runpy.run_path(script, run_name='__main__')
                    self.assertEqual(error.exception.code, 2)
            for extra, filename in (([], 'site.html'),
                                    (['--options', 'options.txt'], 'options.txt')):
                with self.subTest(output_collision=filename):
                    with patch('sys.argv', [script, self.root, '--json-file', filename] + extra):
                        with self.assertRaises(SystemExit) as error:
                            runpy.run_path(script, run_name='__main__')
                    self.assertEqual(error.exception.code, 2)
                    session.request.assert_not_called()

    @patch('site_graph.visualize')
    @patch('crawler.requests.Session')
    def test_cli_accepts_http_without_override(self, session_class, visualize):
        root = 'http://localhost:8080/'
        session = session_class.return_value.__enter__.return_value
        session.request.return_value = response()
        with tempfile.TemporaryDirectory() as directory, contextlib.chdir(directory), \
                patch('sys.argv', ['site_graph.py', root]):
            main()
            self.assertEqual(load_data('crawl.json').resolved_root_url, root)
        self.assertEqual(session.request.call_args.args, ('GET', root))
        visualize.assert_called_once()


class SavedWorkflowTests(unittest.TestCase):
    root = 'https://example.org/site/'
    outside = 'https://outside.test/?query=kept'

    def result(self):
        result = CrawlResult(self.root, self.root, {
            'visit_external': False, 'keep_queries': False, 'workers': 1,
        }, generated_at='2026-09-26T07:00:00Z')
        result.nodes = {
            self.root: CrawlNode(self.root, 'internal', observations=[
                Observation('GET', 200, type='page', discovered=True),
            ]),
            self.outside: CrawlNode(self.outside, 'external'),
        }
        result.edges = {(self.root, self.outside)}
        return result

    def cli(self, *args):
        output = io.StringIO()
        with patch('sys.argv', ['site_graph.py', *map(str, args)]), contextlib.redirect_stdout(output), \
                patch('render.Network', side_effect=lambda **kwargs: Network(
                    **{**kwargs, 'cdn_resources': 'in_line'})):
            main()
        return output.getvalue()

    @patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network'))
    def test_json_rerender_no_network_real_outputs_and_roundtrip(self, request):
        result = self.result()
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, exported = directory / 'input.json', directory / 'copy.json'
            html = directory / 'graph.html'
            options = directory / 'options.txt'
            options.write_text('var options = {"physics": false};')
            write_json(result, source)
            original = source.read_bytes()
            output = self.cli('--from-data-file', source, '--json-file', exported,
                              '--vis-file', html, '--options', options)
            self.assertEqual(load_data(exported).to_dict(), result.to_dict())
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(html.is_file())
            self.assertIn(self.root, html.read_text())
            self.assertIn('"enabled": false', html.read_text())
            self.assertIn('1 checked, 1 unchecked', output)
            self.assertIn('Discovery: complete.', output)
            with contextlib.chdir(directory):
                self.cli('--from-data-file', source, '--vis-file', html)
                self.assertFalse(Path('crawl.json').exists())
            request.assert_not_called()

    @patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network'))
    def test_invalid_files_and_configuration_fail_before_network(self, request):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, options = directory / 'input.json', directory / 'options.txt'
            write_json(self.result(), source)
            output_args = ['--vis-file', str(directory / 'graph.html'),
                           '--json-file', str(directory / 'crawl.json')]
            for flags in (['--workers', '0'], ['--width', '-1'],
                          ['--vis-file', str(directory / 'graph.txt')],
                          ['--options', str(options)],
                          ['--json-file', str(directory / 'absent' / 'crawl.json')],
                          ['--show-buttons', '--options', str(options)]):
                with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.cli(self.root, *output_args, *flags)
            for invalid in ('not JSON', '[]', '{"physics": 5}', '{"physics": {"enabled": "yes"}}'):
                options.write_text(invalid)
                with self.subTest(options=invalid), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.cli(self.root, '--options', options, *output_args)
            for flags in (['--visit-external'], ['--keep-queries'], ['--recheck-external'], [self.root],
                          ['--vis-file', str(source)], ['--json-file', str(source)]):
                with self.subTest(flags=flags), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.cli('--from-data-file', source, *output_args, *flags)
            for invalid in ('{', '{"schema_version": 999}', '[]'):
                source.write_text(invalid)
                with self.subTest(data=invalid), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.cli('--from-data-file', source, *output_args)
            for root in ('', 'ftp://example.org/', 'https://example.org:invalid/'):
                with self.subTest(root=root), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        self.cli(root, *output_args)
            request.assert_not_called()

    def test_summary_does_not_conflate_discovery_http_and_fetch_failures(self):
        result = self.result()
        result.nodes[self.root].observations.append(
            Observation('HEAD', 200, FetchError('timeout', 'body failed'), type='page')
        )
        text = summarize(result)
        self.assertIn('Discovery: complete.', text)
        self.assertIn('1 checked, 1 unchecked; 1 fetch failures', text)
        self.assertIn('2xx=1', text)
        self.assertIn('Broken targets: 1', text)


class GraphControlsTests(unittest.TestCase):
    root = 'https://example.org/'

    def result(self):
        result = CrawlResult(self.root, self.root, {
            'visit_external': False, 'keep_queries': False, 'workers': 1,
        }, generated_at='2026-09-26T07:00:00Z')
        result.nodes[self.root] = CrawlNode(self.root, 'internal', observations=[
            Observation('GET', 200, type='page', discovered=True),
        ])
        for name, observation in [
            ('asset', Observation('GET', 200, type='resource')),
            ('gone', Observation('GET', 404)),
            ('denied', Observation('GET', 403)),
            ('busy', Observation('GET', 503)),
            ('timeout', Observation('GET', 200, FetchError('timeout', 'body failed'))),
        ]:
            url = self.root + name
            result.nodes[url] = CrawlNode(url, 'internal', observations=[observation])
            result.edges.add((self.root, url))
        outside = 'https://outside.test/'
        result.nodes[outside] = CrawlNode(outside, 'external')
        result.edges.add((outside, self.root))
        alias = self.root + 'old'
        result.nodes[alias] = CrawlNode(alias, 'internal', self.root, [
            Observation('GET', 301, redirect_to=self.root),
        ])
        return result

    def render(self, result, interactive=False, **options):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'graph.html'
            args = SimpleNamespace(
                width=1000, height=800,
                show_buttons=False, options=None, only_404=True, prelayout=False,
                interactive_controls=interactive, vis_file=str(filename),
            )
            vars(args).update(options)
            with patch('render.Network', side_effect=lambda **kwargs: Network(
                    **{**kwargs, 'cdn_resources': 'in_line'})), \
                    patch('requests.sessions.Session.request', side_effect=AssertionError('network')):
                visualize(result, args)
            document = filename.read_text()
        data = json.loads(BeautifulSoup(document, 'html.parser').find(id='graph-data').string)
        return document, {node['id']: node for node in data['nodes']}, data['edges']

    @patch('crawler.requests.Session')
    def test_unfollowed_redirect_is_json_evidence_not_a_graph_singleton(self, session_class):
        def request(method, url, **kwargs):
            if url == self.root:
                return response(body='<a href="https://outside.test/0">redirect chain</a>')
            step = int(url.rsplit('/', 1)[1])
            return response(302, headers={'Location': f'https://outside.test/{step + 1}'})

        session_class.return_value.__enter__.return_value.request.side_effect = request
        result = crawl_site(self.root, True, False)
        unfollowed = 'https://outside.test/11'
        failed = 'https://outside.test/10'
        before = copy.deepcopy(result.to_dict())
        validate_crawl_data(before)
        self.assertEqual(result.nodes[unfollowed].observations, [])
        self.assertIsNone(result.nodes[unfollowed].alias_of)
        self.assertEqual(result.nodes[failed].observations[-1].fetch_error.kind, 'redirect')
        self.assertEqual(result.edges, {(self.root, failed)})
        result = from_dict(before)
        for interactive in (False, True):
            _, nodes, edges = self.render(result, interactive)
            self.assertEqual(set(nodes), {self.root, failed})
            self.assertEqual([(edge['from'], edge['to']) for edge in edges], [(self.root, failed)])
        self.assertEqual(result.to_dict(), before)
        self.assertIn('10 aliases.', summarize(result))
        self.assertIn('1 unfollowed redirect destinations', summarize(result))
        # Independently linked targets and disconnected observations are not intermediaries.
        result.edges.add((self.root, unfollowed))
        self.assertIn(unfollowed, result.graph_nodes())
        result.edges.clear()
        result.nodes[unfollowed].observations.append(Observation('HEAD', 200, type='page'))
        disconnected = 'https://disconnected.test/'
        result.nodes[disconnected] = CrawlNode(disconnected, 'external')
        self.assertEqual(set(result.graph_nodes()), {self.root, failed, unfollowed, disconnected})

    def test_context_filters_and_loading_geometry_in_browser(self):
        result = self.result()
        for name in ('section', 'referrer'):
            result.nodes[self.root + name] = CrawlNode(self.root + name, 'internal', observations=[
                Observation('GET', 200, type='page', discovered=True),
            ])
        broken, external_referrer = 'https://broken.test/', 'https://referrer.test/'
        result.nodes[broken] = CrawlNode(broken, 'external', observations=[Observation('HEAD', 503)])
        result.nodes[external_referrer] = CrawlNode(external_referrer, 'external')
        result.edges.update({
            (self.root, self.root + 'section'), (self.root + 'section', self.root + 'referrer'),
            (self.root + 'referrer', self.root + 'section'), (self.root + 'referrer', broken),
            (external_referrer, broken),
        })
        for index in range(101):
            url = f'https://unrelated.test/{index}'
            result.nodes[url] = CrawlNode(url, 'external')
            result.edges.add((self.root, url))
        checks = r"""
<script>
document.addEventListener("DOMContentLoaded", () => {
let failure = "";
try {
  const check = (value, message) => { if (!value) throw Error(message); };
  const pane = document.getElementById("mynetwork");
  const overlay = document.getElementById("loadingBar");
  const equal = (a, b) => Math.abs(a - b) <= 2;
  if (!network.physics.options.enabled || nodes.length === 1) {
    check(overlay.hidden, "Overlay starts hidden even without stabilization events");
  }
  network.emit("stabilizationProgress", {iterations: 1, total: 2});
  const box = pane.getBoundingClientRect(), cover = overlay.getBoundingClientRect();
  const progress = overlay.firstElementChild.getBoundingClientRect();
  check(equal(box.height, HEIGHT), "Requested height has CSS units");
  check(equal(box.width, Math.min(WIDTH, pane.parentElement.clientWidth)),
    "Requested width is bounded by viewport");
  check(equal(box.x, cover.x) && equal(box.y, cover.y) &&
    equal(box.width, cover.width) && equal(box.height, cover.height), "Overlay covers network only");
  check(equal(progress.x + progress.width / 2, cover.x + cover.width / 2) &&
    equal(progress.y + progress.height / 2, cover.y + cover.height / 2), "Progress centered in pane");
  check(progress.width <= cover.width && overlay.querySelector("progress").value === .5,
    "Responsive progress displays correct fraction");
  const toggle = document.getElementById("graph-physics");
  const pill = document.getElementById("graph-physics-control");
  const pillBox = pill.getBoundingClientRect();
  check(pill.parentElement === pane && equal(box.right - pillBox.right, 13) &&
    equal(pillBox.top - box.top, 13), "Physics switch sits inside the top-right corner");
  check(pillBox.left >= box.left && pillBox.bottom <= box.bottom, "Physics switch fits small graphs");
  const switchBox = toggle.getBoundingClientRect();
  check(document.elementFromPoint(switchBox.x + switchBox.width / 2,
    switchBox.y + switchBox.height / 2) === toggle, "Physics switch remains usable while loading");
  check(toggle.getAttribute("role") === "switch" && toggle.labels[0] === pill,
    "Physics switch has an accessible label");
  network.emit("stabilizationIterationsDone");
  check(overlay.hidden, "Completion hides overlay");
  check(network.physics.options.enabled === KEEP_PHYSICS, "Explicit physics settings take precedence");
  network.emit("stabilizationProgress", {iterations: 0, total: 0});
  network.emit("stabilized");
  check(overlay.hidden, "Early stabilization hides overlay");
  const get = id => document.getElementById("graph-" + id);
  if (get("controls") && nodes.length > 1) {
    const change = (id, value) => {
      const input = get(id);
      if (input.type === "checkbox") input.checked = value; else input.value = value;
      input.dispatchEvent(new Event("change"));
    };
    const root = "https://example.org/", broken = "https://broken.test/";
    const positions = JSON.stringify(network.getPositions());
    const styles = () => JSON.stringify(edges.get().map(edge => ({
      color: network.body.edges[edge.id].options.color,
      width: network.body.edges[edge.id].options.width
    })));
    const initialStyles = styles();
    change("error", "5xx");
    check([root, root + "section", root + "referrer", broken, "https://referrer.test/"]
      .every(id => !nodes.get(id).hidden), "Error retains direct referrers, internal paths and root");
    check(nodes.get("https://outside.test/").hidden &&
      nodes.get("https://unrelated.test/0").hidden, "Traversal does not reveal unrelated external nodes");
    check(!edges.get().find(edge => edge.from === root + "referrer" && edge.to === broken).hidden,
      "Broken target's directed incoming edge remains visible");
    check(nodes.get(broken).color === "#FF9F40", "Only-404 coloring does not change 5xx category");
    change("internal", false);
    change("state", "other-checked");
    check(!nodes.get(root).hidden && !nodes.get(root + "referrer").hidden,
      "Context bypasses target and health filters");
    check(get("status").textContent.includes("1 matches, 4 context pages"), "Context counted separately");
    network.emit("click", {nodes: [broken]});
    check(get("status").textContent.includes("2 incoming, 0 outgoing"), "Referrers usable in selection");
    check(edges.get().filter(edge => edge.to === broken).every(edge => edge.color.color === "#7b3294"),
      "Selection highlights incoming edges");
    change("error", "404");
    check(network.getSelectedNodes().length === 0, "Hidden selection cleared");
    get("search").value = "broken.test";
    get("search").dispatchEvent(new Event("input"));
    check(get("matches").options[1].textContent.startsWith("[hidden]"), "Search reflects visibility");
    get("search").dispatchEvent(new KeyboardEvent("keydown", {key: "Enter"}));
    check(get("error").value === "all" && network.getSelectedNodes()[0] === broken,
      "Selecting hidden search match reveals it");
    get("clear").click();
    check(styles() === initialStyles, "Selection styles restored");
    check(JSON.stringify(network.getPositions()) === positions, "Filters do not move nodes");
    change("error", "any");
    check(edges.get().every(edge => edge.hidden ||
      (!nodes.get(edge.from).hidden && !nodes.get(edge.to).hidden)), "No dangling visible edges");
  }
  document.body.dataset.graphTest = "PASS";
} catch (error) {
  failure = error.stack;
  document.body.dataset.graphTest = "FAIL";
  document.body.append(error.stack);
}
fetch("/report", {method: "POST", body: failure || "PASS"});
});
</script>
"""
        isolated = self.result()
        isolated.nodes = {self.root: isolated.nodes[self.root]}
        isolated.edges.clear()
        cases = [
            (False, result, 1000, 800, {'options': read_options(Path(__file__).with_name('options.txt'))}),
            (True, result, 720, 320, {'show_buttons': True}),
            (True, result, 1000, 450, {'options': '{"physics":{"enabled":false}}'}),
            (True, isolated, 240, 180, {'options': '{"physics":{"enabled":false}}'}),
            (False, result, 800, 450, {'options': json.dumps({
                'physics': {'enabled': True, 'stabilization': {'enabled': False}},
                'layout': {'improvedLayout': False}, 'edges': {'smooth': True},
            })}),
        ]
        for interactive, data, width, height, options in cases:
            with self.subTest(interactive=interactive, width=width, options=options):
                document, _, _ = self.render(data, interactive, width=width, height=height, **options)
                script = checks.replace('WIDTH', str(width)).replace('HEIGHT', str(height))
                physics = json.loads(options.get('options', '{}')).get('physics', {})
                script = script.replace('KEEP_PHYSICS', json.dumps(physics.get('enabled', True)))
                self.assertEqual(self.browser_report(document, script), 'PASS')

    def browser_report(self, document, script):
        chrome = shutil.which('chromium') or shutil.which('google-chrome')
        if not chrome:
            candidate = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
            chrome = str(candidate) if candidate.is_file() else None
        if not chrome:
            self.skipTest('Chrome/Chromium required for browser regression')
        # All vis assets are inline; omit unrelated Bootstrap CDN requests.
        document = re.sub(r'<(?:script|link)\b[^>]*(?:src|href)="https://[^>]+>'
                          r'(?:</script>)?', '', document)
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'graph.html'
            filename.write_text(document.replace('</body>', script + '</body>'))
            finished, reports = Event(), []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.end_headers()
                    self.wfile.write(filename.read_bytes())

                def do_POST(self):
                    reports.append(self.rfile.read(int(self.headers['Content-Length'])).decode())
                    self.send_response(204)
                    self.end_headers()
                    finished.set()

            with HTTPServer(('127.0.0.1', 0), Handler) as server:
                thread = Thread(target=server.serve_forever)
                thread.start()
                try:
                    with subprocess.Popen([
                        chrome, '--headless', '--disable-gpu', '--no-first-run',
                        '--window-size=900,1000', '--remote-debugging-port=0',
                        f'--user-data-dir={directory}/profile',
                        f'http://127.0.0.1:{server.server_port}/',
                    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as browser:
                        try:
                            self.assertTrue(finished.wait(30), 'Browser did not report results')
                            self.assertEqual(len(reports), 1)
                            return reports[0]
                        finally:
                            browser.terminate()
                            try:
                                browser.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                browser.kill()
                finally:
                    server.shutdown()
                    thread.join()

    def test_prelayout_and_physics_in_browser(self):
        try:
            import playwright.async_api
        except ImportError:
            self.skipTest('Playwright required for prelayout regression')
        checks = r"""
<script>
document.addEventListener("DOMContentLoaded", async () => {
  const check = (value, message) => { if (!value) throw Error(message); };
  try {
    const physics = document.getElementById("graph-physics");
    check(physics.checked === network.physics.options.enabled, "Physics toggle matches startup options");
    const initial = JSON.parse(document.getElementById("graph-data").textContent);
    check(initial.nodes.every(node => {
      const position = network.getPosition(node.id);
      return position.x === node.x && position.y === node.y;
    }), "Opening preserves every position without another layout");
    check(physics.checked === (initial.options.physics.enabled !== false), "Physics setting retained");
    check(!network.physics.options.stabilization.enabled, "Prelayout skips initial stabilization");
    check(!network.layoutEngine.options.improvedLayout &&
      !network.layoutEngine.options.hierarchical.enabled, "Initial layouts disabled");
    check(!document.getElementById("graph-save"), "No Save layout control");
    if (physics.checked) physics.click();
    await new Promise(resolve => requestAnimationFrame(resolve));
    check(!network.physics.options.enabled, "Physics can be paused");
    check(!document.querySelector('script[src^="lib/"]'), "No relative runtime assets");
    check(document.querySelectorAll("#loadingBar").length === 1, "No duplicated overlay");
    const root = "https://example.org/";
    const position = network.getPosition(root);
    network.focus(root, {scale: 1, animation: false});
    network.redraw();
    const canvas = document.querySelector("canvas"), box = canvas.getBoundingClientRect();
    const point = network.canvasToDOM(position);
    const pointer = (target, type, offset, buttons) => target.dispatchEvent(new PointerEvent(type, {
      bubbles: true, clientX: box.x + point.x + offset, clientY: box.y + point.y + offset,
      buttons, button: 0, pointerId: 1, pointerType: "mouse", isPrimary: true
    }));
    pointer(canvas, "pointerdown", 0, 1);
    pointer(window, "pointermove", 10, 1);
    pointer(window, "pointermove", 50, 1);
    pointer(window, "pointerup", 50, 0);
    check(network.getPosition(root).x !== position.x, "Prelaid nodes remain draggable");
    const paused = JSON.stringify(network.getPositions(nodes.getIds()));
    physics.click();
    check(network.physics.options.enabled, "Physics resumes with the toggle");
    for (let attempt = 0; attempt < 20 &&
        JSON.stringify(network.getPositions(nodes.getIds())) === paused; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    check(JSON.stringify(network.getPositions(nodes.getIds())) !== paused, "Resumed physics moves nodes");
    check(document.getElementById("loadingBar").hidden, "Resume does not repeat hidden initial layout");
    physics.click();
    check(!network.physics.options.enabled, "Physics pauses again");
    network.setOptions({physics: {enabled: true}});
    network.emit("configChange", {physics: {enabled: true}});
    check(physics.checked, "Settings UI synchronizes physics switch");
    physics.click();
    if (document.getElementById("graph-controls")) {
      const error = document.getElementById("graph-error");
      error.value = "404";
      error.dispatchEvent(new Event("change"));
      network.emit("click", {nodes: [root + "gone"]});
      check(nodes.get().some(node => node.hidden), "Filtering works after prelayout");
      document.getElementById("graph-reset").click();
      const search = document.getElementById("graph-search");
      search.value = "gone";
      search.dispatchEvent(new KeyboardEvent("keydown", {key: "Enter"}));
      check(network.getSelectedNodes()[0] === root + "gone", "Search works after prelayout");
    }
    check(!window.injected, "Script-like URL/error text stays inert");
    fetch("/report", {method: "POST", body: "PASS"});
  } catch (error) {
    fetch("/report", {method: "POST", body: error.stack});
  }
});
</script>
"""
        result = self.result()
        hostile = self.root + '</script><script>window.injected=true</script>&\u2028'
        result.nodes[hostile] = CrawlNode(hostile, 'internal', observations=[
            Observation('GET', 200, FetchError('request', hostile)),
        ])
        result.edges.add((self.root, hostile))
        before = result.to_dict()
        for options in ({'show_buttons': True},
                        {'options': '{"physics":{"enabled":false}}'},
                        {'options': '{"layout":{"hierarchical":true},"physics":{"stabilization":false}}'}):
            with self.subTest(options=options):
                _, original_nodes, original_edges = self.render(result, True, **options)
                document, nodes, edges = self.render(result, True, prelayout=True, **options)
                self.assertEqual(edges, original_edges)
                self.assertEqual(self.browser_report(document, checks), 'PASS')
                for node in nodes.values():
                    self.assertEqual({key: value for key, value in node.items()
                                      if key not in ('x', 'y')}, original_nodes[node['id']])
        self.assertEqual(result.to_dict(), before)

    def test_prelayout_is_optional_and_failure_preserves_output(self):
        from prelayout import compute_positions

        with patch.dict('sys.modules', {'playwright.async_api': None}):
            self.render(self.result())
            with self.assertRaisesRegex(ValueError, 'pip install playwright'):
                compute_positions('', [])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'graph.html'
            source = Path(directory) / 'crawl.json'
            write_json(self.result(), source)
            output.write_text('previous graph')
            stdout = io.StringIO()
            with patch('sys.argv', ['site_graph.py', '--from-data-file', str(source),
                                    '--vis-file', str(output), '--prelayout']), \
                    patch('prelayout.compute_positions', side_effect=ValueError('layout failed')), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main()
                self.assertEqual(error.exception.code, 1)
            self.assertNotIn('Saved graph', stdout.getvalue())
            self.assertEqual(output.read_text(), 'previous graph')
            self.assertEqual(set(Path(directory).iterdir()), {source, output})
            positions = {node_id: {'x': 0, 'y': 0} for node_id in self.result().graph_nodes()}
            with patch('prelayout.compute_positions', return_value=positions), \
                    patch('render.os.replace', side_effect=OSError('disk failure')):
                with self.assertRaisesRegex(OSError, 'disk failure'):
                    self.render(self.result(), prelayout=True, vis_file=str(output))
            self.assertEqual(output.read_text(), 'previous graph')
            self.assertEqual(set(Path(directory).iterdir()), {source, output})

    def test_prelayout_validates_positions_and_closes_browser_on_failure(self):
        try:
            from playwright.async_api import Error
        except ImportError:
            self.skipTest('Playwright required for prelayout regression')
        from prelayout import compute_positions

        page = MagicMock(set_content=AsyncMock(), evaluate=AsyncMock())
        browser = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock())
        with patch('playwright.async_api.async_playwright') as factory:
            chromium = factory.return_value.__aenter__.return_value.chromium
            chromium.launch = AsyncMock(return_value=browser)
            for value in (None, {}, {'wrong': {'x': 0, 'y': 0}},
                          {self.root: {'x': float('nan'), 'y': 0}},
                          {self.root: {'x': True, 'y': 0}},
                          {self.root: {'x': 0, 'y': 0, 'script': 'bad'}}):
                page.evaluate.return_value = value
                with self.subTest(positions=value), self.assertRaisesRegex(ValueError, 'invalid node positions'):
                    compute_positions('network = new vis.Network(container, data, options);', [self.root])
            for error in (TimeoutError(), Error('Executable does not exist')):
                page.evaluate.side_effect = error
                with self.subTest(error=error), self.assertRaisesRegex(ValueError, 'playwright install chromium'):
                    compute_positions('network = new vis.Network(container, data, options);', [self.root])
            self.assertEqual(browser.close.await_count, 8)

    def test_opt_in_preserves_graph_coloring_and_drawing_options(self):
        result = self.result()
        before = copy.deepcopy(result.to_dict())
        for drawing_options in (None, '{"layout":{"improvedLayout":false}}',
                                '{"edges":{"smooth":{"enabled":true,"type":"dynamic"}}}'):
            document, _, _ = self.render(result, options=drawing_options)
            actual = json.loads(BeautifulSoup(document, 'html.parser').find(id='graph-data').string)['options']
            if drawing_options is None:
                self.assertFalse(actual['edges']['smooth']['enabled'])
                self.assertEqual(actual['physics']['stabilization']['iterations'], 1000)
            else:
                expected = json.loads(drawing_options)
                expected.setdefault('edges', {}).setdefault('smooth', False)
                self.assertEqual(actual, expected)
        for options in ({'options': read_options(Path(__file__).with_name('options.txt'))},
                        {'show_buttons': True}):
            with self.subTest(options=options):
                default, plain, plain_edges = self.render(result, **options)
                document, nodes, edges = self.render(result, True, **options)
                self.assertNotIn('id="graph-controls"', default)
                self.assertIn('id="graph-controls"', document)
                self.assertEqual(nodes.keys(), plain.keys())
                self.assertEqual(edges, plain_edges)
                self.assertNotIn(self.root + 'old', nodes)
                self.assertEqual(nodes[self.root]['crawl']['aliases'], [self.root + 'old'])
                for url, node in nodes.items():
                    self.assertEqual(node['color'], plain[url]['color'])
                    self.assertEqual(node['label'], '')
                    self.assertNotIn('crawl', plain[url])
                self.assertEqual(nodes[self.root + 'gone']['color'], '#FF0800')
                self.assertEqual(nodes[self.root + 'busy']['color'], '#0072BB')
                self.assertEqual(nodes[self.root + 'busy']['crawl']['errors'], ['5xx'])
                if options.get('show_buttons'):
                    self.assertIn('id="config"', document)
                else:
                    self.assertIn('"gravitationalConstant": -10000', document)
        self.assertEqual(result.to_dict(), before)

    def test_untrusted_urls_messages_and_options_are_not_html_or_script(self):
        attack = '"><img src=x onerror=alert(1)></script><script>alert(2)</script>&\u2028'
        url = self.root + attack
        result = self.result()
        result.nodes[url] = CrawlNode(url, 'internal', observations=[
            Observation('GET', fetch_error=FetchError('request', attack)),
        ])
        result.edges.add((self.root, url))
        for interactive in (False, True):
            document, nodes, _ = self.render(
                result, interactive, options=json.dumps({'locale': attack})
            )
            self.assertNotIn(attack, document)
            self.assertNotIn('<script>alert(2)</script>', document)
            tooltip = BeautifulSoup(nodes[url]['title'], 'html.parser')
            self.assertFalse(tooltip.find(['img', 'script']))
            self.assertEqual(tooltip.a['href'], url)
            self.assertIn(attack, tooltip.get_text())
            if interactive:
                self.assertIn(attack, nodes[url]['crawl']['detail'])
        for unsafe in ('javascript:alert(1)', 'data:text/html,bad', 'file:///etc/passwd',
                       'https://example.org/\nunsafe', 'https://[bad'):
            self.assertIsNone(BeautifulSoup(url_link(unsafe), 'html.parser').find('a'))

    def test_explicit_check_states_errors_resources_and_isolated_roots(self):
        _, nodes, _ = self.render(self.result(), True)
        self.assertEqual(nodes[self.root]['crawl']['state'], 'success')
        for name, category in [('gone', '404'), ('denied', 'other-4xx'),
                               ('busy', '5xx'), ('timeout', 'timeout')]:
            self.assertEqual(nodes[self.root + name]['crawl']['errors'], [category])
            self.assertEqual(nodes[self.root + name]['crawl']['state'], 'other-checked')
        self.assertTrue(nodes[self.root + 'asset']['crawl']['resource'])
        self.assertEqual(nodes['https://outside.test/']['shape'], 'triangle')
        self.assertEqual(nodes['https://outside.test/']['crawl']['state'], 'unchecked')
        result = self.result()
        result.nodes = {self.root: result.nodes[self.root]}
        result.edges.clear()
        _, nodes, edges = self.render(result, True)
        self.assertEqual(edges, [])
        self.assertEqual(list(nodes), [self.root])


if __name__ == '__main__':
    unittest.main()
