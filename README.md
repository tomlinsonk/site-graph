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

Use `--visit-external` to check external links, with `--workers N` to visit N external sites in parallel (default: 4).

Each crawl saves `site.html` and [versioned crawl data](CRAWL_JSON.md) in `crawl.json`.
Use `--vis-file` and `--json-file` to choose output paths.
Rerender offline with `--from-data-file crawl.json`; add `--recheck-external --json-file checked.json` to check saved external targets and save the updates.
Add `--interactive-controls` for client-side URL search, filters, a check-state legend, and directed neighbor highlighting.
Error filters retain referring pages and upstream internal paths so broken links remain traceable; `--only-404` changes coloring, not filter categories.
JSON v1 is now the only saved format. Pickle support, `--data-file`, and the old
tuple-returning Python API have been removed; existing v1 JSON files still work.

To see a graph of a local files, serve the files using a simple local HTTP server such as [Twisted](https://github.com/twisted/twisted) (in Python), usage: `twistd -no web --path=[path to files]`, or [http-server](https://github.com/http-party/http-server) (in Node.js), usage: `http-server [path to files]`, and use the resulting URL, for example: `python3 site_graph.py --force http://localhost:8080/`

## Contributing
The code has four parts: `site_graph.py` (CLI), `crawler.py` (HTTP and discovery),
`crawl_result.py` (model, JSON validation/storage, summaries), and `render.py`
(HTML and matrix exports). The two HTML fragments provide layout and optional controls.
Run `python3 -m unittest` for the test suite; browser checks also run when Chrome/Chromium is available.

This code is under a MIT License. Feel free to make pull requests if there are some features you'd like included (or bugs you'd like fixed).
