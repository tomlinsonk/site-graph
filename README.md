# Website link graph visualization

![](example.png)

- [my blog post about this project](https://www.kirantomlinson.com/post/site-graph/)
- [a live example on my website](https://www.kirantomlinson.com/graph/)

## Dependencies
python3
- bs4
- pyvis
- networkx
- requests
- scipy

## Setup with Virtual Environment (recommended)

```
git clone https://github.com/tomlinsonk/site-graph.git
cd site-graph
python3 -m venv venv
source venv/bin/activate
pip3 install -r requirements.txt
```

To use the project in a new terminal session:
```
source venv/bin/activate
```

To deactivate the virtual environment:
```
deactivate
```

## Running

**After activating the virtual environment:**

```
python3 site_graph.py https://www.kirantomlinson.com/
```
To see site of interest for you, just change the URL.

To see more options, run:
```python3 site_graph.py -h```

Blue nodes are internal pages, green nodes are internal resource files (anything that isn't HTML), orange nodes are external pages, and red nodes are pages with errors. Hover over nodes to see URLs and specific errors (e.g. 404, 500, timeout).

To see a graph of a local files, serve the files using a simple local HTTP server such as [Twisted](https://github.com/twisted/twisted) (in Python), usage: `twistd -no web --path=[path to files]`, or [http-server](https://github.com/http-party/http-server) (in Node.js), usage: `http-server [path to files]`, and use the resulting URL, for example: `python3 site_graph.py --force http://localhost:8080/`

### Crawl behavior and performance

Internal pages are discovered serially with a reusable HTTP session and a single GET per page, rather than HEAD followed by GET. Non-HTML resources are not downloaded in full. External targets are recorded without being checked unless you pass `--visit-external`. With that flag, external checks run after discovery using four workers, each with its own reusable session. Use `--workers 1` for serial checks or adjust the number for your site:

```sh
python3 site_graph.py --visit-external --workers 4 --only-404 \
  https://www.kirantomlinson.com --options options.txt
```

Availability checks use HEAD, falling back to a streamed GET on HTTP 405 or 501 without consuming the body. Requests have connect/read timeouts and up to two retries for connection errors, timeouts while requesting headers, and HTTP 429/500/502/503/504. Retries honor `Retry-After`; if it requests more than 30 seconds, the response is reported instead of retrying early or waiting indefinitely. Redirect chains are limited to ten hops. An internal redirect outside the crawl scope is checked but its links are not traversed.

The resolved root URL establishes the crawl scope: the same scheme, hostname, effective port, and root path boundary. A root at `/~kt/` includes `/~kt/page`, but not `/~kt-other/`. Relative links and `<base href>` use the final response URL. Fragments are always removed; internal query strings are removed unless `--keep-queries` is set, while external query strings are preserved. Redirect destinations retain their queries. Trailing-slash variants remain separate unless a redirect proves they are aliases. Incoming edges, resource classification, and errors follow observed aliases.

HTML and the existing pickle/re-render workflow are preserved. New pickle files store the resolved root for consistent rendering; existing pickle files remain readable. `--only-404` only changes coloring: other HTTP and fetch errors remain in the saved data and tooltips. An unusable root produces an error instead of an empty successful crawl.

## Tests

Run the focused, network-independent regression tests with `python3 -m unittest -q`.

## Contributing
This code is under a MIT License. Feel free to make pull requests if there are some features you'd like included (or bugs you'd like fixed).
