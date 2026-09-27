# Crawl JSON v1

Crawl data is saved to `crawl.json`, or the path given by `--json-file`.
Use `--from-data-file crawl.json` to draw the graph without fetching any URLs.
Input/output paths must differ.

Top-level fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1`. |
| `requested_root_url`, `resolved_root_url` | Normalized requested URL and final URL of the initial root GET; the latter defines internal scope. |
| `generated_at` | UTC ISO 8601 timestamp ending in `Z`, recorded after crawling. |
| `settings` | `visit_external`, `keep_queries` (booleans), `workers` (positive integer). |
| `nodes` | URL-sorted records, including an isolated root, unchecked link targets, and redirect aliases. |
| `edges` | Unique directed `{source, target}` URL-ID pairs, sorted by source then target; endpoints are canonicalized. |
| `discovery` | `{complete, reasons}` describes internal link discovery, not link availability. Each reason is `{url, reason}`. |
| `checking` | `{checked, unchecked}` counts distinct URL nodes, including aliases; failures count as checked. |

Each node has `id` (URL), `scope` (`internal`/`external`), `alias_of` (canonical URL
or null), `type` (`page`/`resource`/`unknown`), `check_state`
(`checked`/`unchecked`), `http_status` (integer or null), `fetch_error` (null or
`{kind, message}`), and `observations`. Error kinds are `timeout`, `connection`,
`request`, and `redirect`; an HTTP 404/500 is a status, not a fetch error.

Observations remain attached to the URL actually requested, in request order.
Each has `method` (`GET`/`HEAD`), `http_status`, `fetch_error`, `redirect_to`
(URL or null), `type`, and `discovered` (whether internal HTML was obtained for
link parsing). Redirect hops and HEAD-to-GET fallbacks are retained; retry
attempts are represented by their final outcome, not an HTTP wire log.
Node status/error/type summarize the latest observation; unchecked nodes have
null status/error and unknown type. A body-read failure can have both an HTTP
status and a fetch error. Missing content type is unknown, not a guessed page.
Aliases keep their own observations even when graph edges merge.

Discovery is complete when every known non-alias internal URL either yielded
HTML for parsing, was successfully identified as a resource, or returned a
terminal 404/410. Other cases report `fetch_error`, `http_error`,
`unknown_content_type`, or `not_discovered`. Unchecked external links do not
make discovery incomplete. An external check that redirects to previously
undiscovered internal HTML does: checking does not expand the crawl frontier.
Completeness describes this anchor-link crawler's observations, not JavaScript,
unlinked pages, or a guarantee that the website remained unchanged.

## Python API

`crawler.crawl_site(...)` returns a `crawl_result.CrawlResult`.
Its `to_dict()` method produces the JSON fields described above.
`crawl_result.write_json(result, filename)` validates the data and replaces the file
after writing it in full. `load_data(path)` and `from_dict(data)` validate saved data
and return a `CrawlResult`.

`generated_at=` on `crawl_site` accepts a fixed UTC timestamp for reproducibility.
`graph_nodes()` excludes aliases and unfollowed
redirect destinations with no observations or link edges (for example, beyond the
redirect limit). These remain in JSON; isolated roots and other disconnected
observations remain in the graph.
No drawing settings, colors, or tooltips enter JSON.

`render.visualize(result, args)` draws the graph using the command-line options in `args`.

Console summaries count canonical nodes/edges, latest HTTP categories, unchecked
targets and fetch failures separately from discovery completeness. Response and
fetch-failure counts may overlap; only broken targets list their referring pages.
