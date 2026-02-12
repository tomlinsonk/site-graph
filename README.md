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

To see a graph of a local files, serve the files using a simple local HTTP server such as [Twisted](https://github.com/twisted/twisted) (in Python), usage: `twistd -no web --path=[path to files]`, or [http-server](https://github.com/http-party/http-server) (in Node.js), usage: `http-server [path to files]`, and use the resulting URL, for example: `python3 site_graph.py http://localhost:8080/`

## Contributing
This code is under a MIT License. Feel free to make pull requests if there are some features you'd like included (or bugs you'd like fixed).
