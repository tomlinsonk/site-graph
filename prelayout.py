"""Optional render-time layout using the same vis-network as the HTML viewer."""

import asyncio
import math


# Enable one bounded stabilization, including when interactive stabilization is off.
# Attach before the first asynchronous physics batch; hierarchical layout runs in
# the constructor. Only this private browser document receives these changes.
INITIALIZE = """
const physics = options.physics ?? {};
options.physics = {...physics, stabilization: {...physics.stabilization, enabled: true}};
network = new vis.Network(container, data, options);
window.prelayoutReady = new Promise(resolve => {
  const finish = () => {
    network.stopSimulation();
    resolve(network.getPositions(nodes.getIds()));
  };
  if (physics.enabled === false || nodes.length === 0) finish();
  else {
    network.once("stabilizationIterationsDone", finish);
    network.once("stabilized", finish);
  }
});
"""


def compute_positions(document, node_ids):
    try:
        from playwright.async_api import Error, async_playwright
    except ImportError as error:
        raise ValueError('--prelayout requires Playwright: run `python3 -m pip install playwright` '
                         'then `python3 -m playwright install chromium`') from error

    constructor = 'network = new vis.Network(container, data, options);'
    if document.count(constructor) != 1:
        raise ValueError('--prelayout: unsupported graph template')
    document = document.replace(constructor, INITIALIZE)

    async def run():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(timeout=30_000)
            try:
                page = await browser.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('requestfailed', lambda request: errors.append(
                    f'{request.url}: {request.failure}'
                ) if request.resource_type in ('script', 'stylesheet') else None)
                page.on('response', lambda response: errors.append(
                    f'{response.url}: HTTP {response.status}'
                ) if response.status >= 400 and response.request.resource_type in ('script', 'stylesheet') else None)
                await page.set_content(document, wait_until='load', timeout=30_000)
                positions = await page.evaluate('() => window.prelayoutReady')
                if errors:
                    raise ValueError('--prelayout: browser script or asset failed: ' + '; '.join(errors))
                return positions
            finally:
                await browser.close()

    async def bounded():
        return await asyncio.wait_for(run(), timeout=120)

    try:
        positions = asyncio.run(bounded())
    except (Error, TimeoutError) as error:
        raise ValueError('--prelayout failed (30s startup / 120s total limit). '
                         'Check CDN access and run `python3 -m playwright install chromium`. '
                         f'{error}') from error
    if not isinstance(positions, dict) or set(positions) != set(node_ids) or any(
        not isinstance(position, dict) or set(position) != {'x', 'y'} or any(
            type(value) not in (int, float) or not math.isfinite(value)
            for value in position.values()
        ) for position in positions.values()
    ):
        raise ValueError('--prelayout: browser returned incomplete or invalid node positions')
    return positions
