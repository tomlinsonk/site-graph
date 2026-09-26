import unittest
from collections import Counter
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import requests

from site_graph import crawl, fetch, is_internal, resolve_url, visualize


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
        edges, errors, resources, resolved_root = crawl(origin + '/start', False, False)
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
        self.assertIn('Redirect limit', fetch(session, 'https://example.org/').error)
        self.assertEqual(session.request.call_count, 11)

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


if __name__ == '__main__':
    unittest.main()
