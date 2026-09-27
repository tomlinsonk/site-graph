"""HTTP fetching and link discovery."""

import time
import urllib.parse
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Optional

import requests
from bs4 import BeautifulSoup

from crawl_result import CrawlResult, FetchError, Observation, is_internal, utc_now


def resolve_url(href, base_url='', site_url=None, keep_queries=True):
    try:
        parts = urllib.parse.urlsplit(urllib.parse.urljoin(base_url, href.strip()))
        if parts.scheme not in ('http', 'https'):
            return None
        if not parts.hostname:
            raise ValueError('missing hostname')
        host = parts.hostname.lower()
        host = f'[{host}]' if ':' in host else host
        port = parts.port
        if port is not None and port != (443 if parts.scheme == 'https' else 80):
            host += f':{port}'
        if '@' in parts.netloc:
            host = parts.netloc.rsplit('@', 1)[0] + '@' + host
        url = parts._replace(netloc=host, path=parts.path or '/', fragment='').geturl()
        if not keep_queries and site_url is not None and is_internal(url, site_url):
            url = urllib.parse.urlsplit(url)._replace(query='').geturl()
        return url
    except ValueError as error:
        print(f'Ignoring invalid URL {href!r}: {error}')
        return None


def request_with_retries(session, method, url):
    for attempt in range(3):
        delay = 0.5 * 2 ** attempt
        try:
            response = session.request(
                method, url, allow_redirects=False, stream=True, timeout=(5, 10)
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt == 2:
                raise
        else:
            if attempt == 2 or response.status_code not in (429, 500, 502, 503, 504):
                return response
            retry_after = response.headers.get('Retry-After')
            if retry_after:
                try:
                    delay = int(retry_after)
                except ValueError:
                    try:
                        delay = parsedate_to_datetime(retry_after).timestamp() - time.time()
                    except (ValueError, TypeError, OverflowError):
                        print(f'Ignoring invalid Retry-After header from {url}: {retry_after!r}')
                # Do not retry earlier than requested or stall indefinitely.
                if delay > 30:
                    return response
            response.close()
        time.sleep(max(0, delay))


@dataclass
class FetchResult:
    url: str
    text: Optional[str] = None
    observations: list[tuple[str, Observation]] = field(default_factory=list)


def fetch(session, url, discover=False, site_url=None):
    result = FetchResult(url)
    method = 'GET' if discover else 'HEAD'
    redirects = 0
    try:
        while True:
            observation = Observation(method)
            result.observations.append((result.url, observation))
            with request_with_retries(session, method, result.url) as response:
                status = observation.http_status = response.status_code
                if method == 'HEAD' and status in (405, 501):
                    method = 'GET'
                    continue
                if response.is_redirect:
                    target = resolve_url(response.headers['Location'], result.url)
                    observation.redirect_to = target
                    if target is None or redirects >= 10:
                        observation.fetch_error = FetchError('redirect',
                            'Redirect limit exceeded' if target else
                            'Invalid or unsupported redirect destination')
                        return result
                    redirects += 1
                    result.url = target
                    method = 'GET' if discover and (
                        site_url is None or is_internal(target, site_url)
                    ) else 'HEAD'
                    continue
                if 300 <= status < 400:
                    observation.fetch_error = FetchError(
                        'redirect', 'Redirect response without a usable destination'
                    )
                content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
                is_html = content_type in ('text/html', 'application/xhtml+xml')
                observation.type = 'page' if is_html else ('resource' if content_type else 'unknown')
                if discover and is_html and 200 <= status < 300 and (
                    site_url is None or is_internal(result.url, site_url)
                ):
                    result.text = response.text
                    observation.discovered = True
                return result
    except requests.exceptions.RequestException as error:
        kind = ('timeout' if isinstance(error, requests.exceptions.Timeout) else
                'connection' if isinstance(error, requests.exceptions.ConnectionError) else 'request')
        observation.fetch_error = FetchError(kind, f'{type(error).__name__}: {error}')
        return result


def canonical_url(url, aliases):
    seen = set()
    while url in aliases and url not in seen:
        seen.add(url)
        url = aliases[url]
    return url


def check_external_batch(urls):
    with requests.Session() as session:
        results = []
        for url in urls:
            print('Checking', url)
            results.append(fetch(session, url))
        return results


def check_external_targets(targets, workers):
    if targets:
        count = min(workers, len(targets))
        batches = [targets[index::count] for index in range(count)]
        with ThreadPoolExecutor(max_workers=count) as executor:
            for results in executor.map(check_external_batch, batches):
                yield from results


def record_fetch(result, page, aliases):
    # A cookie-setting redirect can legitimately return to an earlier URL.
    aliases.pop(page.url, None)
    for url, observation in page.observations:
        result.node(url).observations.append(observation)
        if observation.redirect_to is not None:
            result.node(observation.redirect_to)
            if url != page.url:
                aliases[url] = page.url
    error = result.nodes[page.url].error()
    if error is not None:
        print(f'{error} ERROR while visiting {page.url}')


def canonicalize_result(result, aliases):
    result.edges = {(canonical_url(source, aliases), canonical_url(target, aliases))
                    for source, target in result.edges}
    for url, item in result.nodes.items():
        canonical = canonical_url(url, aliases)
        item.alias_of = canonical if canonical != url else None


def crawl_site(url, visit_external=False, keep_queries=False, workers=4, *, generated_at=None):
    if type(workers) is not int or workers < 1:
        raise ValueError('workers must be at least 1')
    url = resolve_url(url)
    if url is None:
        raise ValueError('site URL must be an absolute HTTP or HTTPS URL')
    visited = set()
    result = CrawlResult(url, url, {
        'visit_external': visit_external, 'keep_queries': keep_queries, 'workers': workers,
    })
    aliases = {}
    external_targets = set()
    with requests.Session() as session:
        root = fetch(session, url, discover=True)
        if root.text is None:
            observation = root.observations[-1][1]
            error = observation.fetch_error.message if observation.fetch_error else observation.http_status
            raise ValueError(f'Cannot crawl root {root.url}: {error} (HTML required)')
        site_url = result.resolved_root_url = root.url
        to_visit = deque([site_url])
        scheduled = {site_url}

        while to_visit:
            url = canonical_url(to_visit.popleft(), aliases)
            if url in visited:
                continue
            print('Visiting', url)
            page = root if url == site_url else fetch(session, url, discover=True, site_url=site_url)
            record_fetch(result, page, aliases)
            visited.update(url for url, _ in page.observations)
            if page.text is None:
                continue
            soup = BeautifulSoup(page.text, 'html.parser')
            base = soup.find('base', href=True)
            base_url = (resolve_url(base['href'], page.url) or page.url) if base else page.url
            for link in soup.find_all('a', href=True):
                target = resolve_url(link['href'], base_url, site_url, keep_queries)
                if target is None:
                    continue
                result.node(target)
                result.edges.add((page.url, target))
                target = canonical_url(target, aliases)
                if is_internal(target, site_url):
                    if target not in scheduled and target not in visited:
                        scheduled.add(target)
                        to_visit.append(target)
                else:
                    external_targets.add(target)

    if visit_external:
        targets = sorted({canonical_url(url, aliases) for url in external_targets} - visited)
        for page in check_external_targets(targets, workers):
            record_fetch(result, page, aliases)

    canonicalize_result(result, aliases)
    result.generated_at = generated_at if generated_at is not None else utc_now()
    return result
