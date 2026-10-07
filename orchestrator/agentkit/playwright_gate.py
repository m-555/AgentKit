"""Opt-in host preparation: browser checks must serve the assigned checkout."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
from contextlib import contextmanager
from pathlib import Path

from .locking import atomic_write


@contextmanager
def prepared(project, workdir, command):
    recipes = project.raw.get('playwright_checks', [])
    if (not isinstance(recipes, list) or any(not isinstance(item, dict)
            or not isinstance(item.get('match_command'), str) or not item['match_command']
            for item in recipes)):
        raise ValueError('Browser preparation recipes require nonempty match_command strings')
    selected = [item for item in recipes if item['match_command'] in command]
    if not selected:
        yield command
        return
    if len(selected) != 1:
        raise ValueError('Browser check matches multiple preparation recipes')
    if any(part in command for part in ('&&', '||', ';', '\n', '|')) or '--config' in command:
        raise ValueError('Browser preparation requires a single command without a config override')
    recipe = selected[0]
    work = Path(workdir).resolve(strict=True)
    config = work / recipe['config']
    if not config.resolve(strict=True).is_relative_to(work) or not config.is_file():
        raise ValueError('Browser config must be a file inside the assigned checkout')
    server = recipe['server_command']
    if not isinstance(server, str) or '{port}' not in server or not server.strip():
        raise ValueError('Private browser server command must declare {port}')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    # A race for this released port fails closed: reuseExistingServer is false,
    # and the project's explicit server recipe uses strict binding.
    key = hashlib.sha256(str(work).encode()).hexdigest()[:24]
    target = project.root / '.ai/runtime/browser-checks' / key / 'playwright.config.mjs'
    base_url = f'http://127.0.0.1:{port}'
    output = target.parent / 'test-results'
    payload = (
        f"import original from {json.dumps(config.resolve().as_uri())};\n"
        "import { resolve } from 'node:path';\n"
        f"const directory = {json.dumps(str(config.parent))};\n"
        f"const baseURL = {json.dumps(base_url)};\n"
        f"const htmlOutput = {json.dumps(str(target.parent / 'html-report'))};\n"
        "const reporter = Array.isArray(original.reporter) ? original.reporter.map(entry =>\n"
        "  Array.isArray(entry) && entry[0] === 'html'\n"
        "    ? ['html', { ...entry[1], outputFolder: htmlOutput }] : entry)\n"
        "  : original.reporter === 'html' ? [['html', { outputFolder: htmlOutput }]] : original.reporter;\n"
        "export default { ...original, reporter,\n"
        "  projects: original.projects?.map(project => ({ ...project, use: { ...project.use, baseURL } })),\n"
        "  testDir: resolve(directory, original.testDir || '.'),\n"
        f"  outputDir: {json.dumps(str(output))},\n"
        "  use: { ...original.use, baseURL },\n"
        "  webServer: { ...original.webServer,\n"
        f"    command: {json.dumps(server.replace('{port}', str(port)))},\n"
        "    cwd: directory, url: baseURL, reuseExistingServer: false,\n"
        "  },\n"
        "};\n"
    )
    if os.name == 'nt':
        if any(char in str(target) for char in ('%', '!', '"', '\r', '\n')):
            raise ValueError('Browser config path cannot be safely quoted for Windows shell')
        quoted = '"' + str(target) + '"'
    else:
        quoted = shlex.quote(str(target))
    atomic_write(target, payload)
    yield command + ' --config ' + quoted
