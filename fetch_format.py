"""Cache specification pages or fetch and render a cXML release.

Originals are cached under untracked/cxml/. Review a rendered snapshot before
copying it to the repository root. Commit the snapshot separately from tooling.

    uv run fetch_format.py fetch-pages
    uv run fetch_format.py fetch --history
    uv run fetch_format.py render 1.2.072 untracked/rendered/1.2.072
    uv run fetch_format.py extract-zugferd untracked/ZUGFeRD_2.5.2_EN.zip untracked/zugferd
"""

import argparse
import json
import sqlite3
import sys
import tomllib
from contextlib import closing
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath, PureWindowsPath
from re import IGNORECASE, fullmatch, search, split, sub
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

import pymupdf
import pymupdf4llm
from bs4 import BeautifulSoup
from markdownify import markdownify

from response_cache import Cache, is_source_url, replace, source_basename, validate

ROOT = Path(__file__).resolve().parent
UPSTREAM = ROOT / 'untracked' / 'cxml'
INDEX = 'https://www.cxml.org/downloads.html'
LICENSE = 'https://www.cxml.org/license.html'
HOSTS = {'cxml.org', 'www.cxml.org', 'xml.cxml.org'}
RELEASE_NOTES = 'ReleaseNotes.html'
RELEASE_NOTES_PAGE = 'release-notes.html'
TEXT_EXTENSIONS = {'.dtd', '.mod', '.txt', '.xml'}


def read_sources(config: Path) -> dict[str, str]:
    entries = tomllib.loads(config.read_text(encoding='utf-8')).get('sources')
    if not isinstance(entries, list) or not entries:
        raise ValueError(f'Expected a nonempty sources list in {config}')
    sources: dict[str, str] = {}
    names: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            url, override = entry, None
        elif isinstance(entry, dict) and entry.keys() <= {'url', 'filename'}:
            url, override = entry.get('url'), entry.get('filename')
        else:
            raise ValueError(f'Expected a URL or a URL with a filename override: {entry!r}')
        if not isinstance(url, str) or (override is not None and not isinstance(override, str)):
            raise ValueError(f'Expected strings for url and filename: {entry!r}')
        parts = urlsplit(url)
        if not parts.hostname or parts.fragment or not is_source_url(url, {parts.netloc}):
            raise ValueError(f'Expected an HTTPS URL without credentials or a fragment: {url}')
        filename = source_basename(url) if override is None else override
        if (
            not filename
            or filename.endswith(('.', ' '))
            or search(r'[\\/:*?"<>|\x00-\x1f]', filename)
            or PureWindowsPath(filename).is_reserved()
        ):
            raise ValueError(f'Set a safe filename override for {url}: {filename!r}')
        if url in sources or filename.casefold() in names:
            raise ValueError(f'Duplicate source URL or filename: {url} ({filename})')
        sources[url] = filename
        names.add(filename.casefold())
    return sources


def fetch_pages(config: Path, *, offline: bool) -> None:
    sources = read_sources(config)
    cache_path = ROOT / '.cache' / 'responses.sqlite3'
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(cache_path, timeout=60)) as connection:
        cache = Cache(connection, {urlsplit(url).netloc for url in sources}, offline=offline)
        for url, filename in sources.items():
            body = cache.get(url, filename='page.html')
            validate('page.html', body)
            replace(ROOT / 'untracked' / 'pages' / filename, body)
            print(f'{filename}: {len(body)} bytes')  # noqa: T201


def is_official_url(url: str) -> bool:
    return is_source_url(url, HOSTS)


def is_translated_pdf(filename: str) -> bool:
    return search(r'_[a-z]{2}(?:[-_][a-z]{2})?\.pdf$', filename, IGNORECASE) is not None


def collect_downloads(body: bytes) -> dict[str, dict[str, str]]:
    """Collect document links under each version heading."""
    releases: dict[str, dict[str, str]] = {}
    sources: dict[str, str] | None = None
    for tag in BeautifulSoup(body.decode(), 'html.parser').find_all(('h2', 'a')):
        if tag.name == 'h2':
            if match := search(r'\bVersion (\d+\.\d+\.\d+)\b', tag.get_text()):
                releases[match[1]] = sources = {}
            else:
                sources = None
            continue
        href = tag.get('href')
        if sources is None or not isinstance(href, str) or not href:
            continue
        url = urljoin(INDEX, href)
        path = urlsplit(url).path
        if (
            is_official_url(url)
            and not is_translated_pdf(path)
            and (path.endswith(('.pdf', '.zip')) or path == f'/{RELEASE_NOTES_PAGE}')
        ):
            sources[PurePosixPath(path).name] = url
    return releases


def find_release_notes(body: bytes) -> str | None:
    """Find the release notes embedded in the landing page."""
    frames = BeautifulSoup(body.decode(), 'html.parser').find_all(
        'iframe', title='cXML Release Notes'
    )
    source = frames[-1].get('src') if frames else None
    return source if isinstance(source, str) else None


def manifest_bytes(version: str, records: dict[str, dict[str, str | int]]) -> bytes:
    lines = [f'version = "{version}"']
    for filename, record in sorted(records.items()):
        lines.extend(('', f'[files.{json.dumps(filename, ensure_ascii=False)}]'))
        lines.extend(
            f'{key} = {json.dumps(value, ensure_ascii=False)}' for key, value in record.items()
        )
    return ('\n'.join(lines) + '\n').encode()


def release_files(directory: Path) -> dict[str, dict[str, str | int]]:
    manifest = directory / 'manifest.toml'
    if manifest.exists():
        return tomllib.loads(manifest.read_text(encoding='utf-8'))['files']
    legacy = directory / 'manifest.json'
    return json.loads(legacy.read_bytes())['files'] if legacy.exists() else {}


def mirror(cache: Cache, version: str, sources: dict[str, str], *, refresh: bool) -> None:
    directory = UPSTREAM / version
    known = release_files(directory)
    documents = {}
    records: dict[str, dict[str, str | int]] = {}
    pending = dict(sorted(sources.items()))
    while pending:
        filename, url = next(iter(pending.items()))
        del pending[filename]
        path = directory / filename
        record = known.get(filename, {})
        seed = path.read_bytes() if path.exists() and record.get('url') == url else None
        if seed is not None and sha256(seed).hexdigest() != record['sha256']:
            seed = None
        body = cache.get(url, refresh=refresh, release=version, seed=seed)
        validate(filename, body)
        documents[path] = body
        records[filename] = {'bytes': len(body), 'sha256': sha256(body).hexdigest(), 'url': url}
        if filename == RELEASE_NOTES_PAGE and (source := find_release_notes(body)):
            notes_url = urljoin(url, source)
            if notes_url != url:
                pending[PurePosixPath(urlsplit(notes_url).path).name] = notes_url
    for path, body in documents.items():
        replace(path, body)
    replace(directory / 'manifest.toml', manifest_bytes(version, records))
    print(f'cXML {version}: verified {len(records)} documents')  # noqa: T201


def fetch(*, history: bool, offline: bool, refresh: bool) -> None:
    cache_path = ROOT / '.cache' / 'responses.sqlite3'
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(cache_path, timeout=60)) as connection:
        cache = Cache(connection, HOSTS, offline=offline)
        index = UPSTREAM / 'downloads.html'
        body = cache.get(
            INDEX, refresh=refresh, seed=index.read_bytes() if index.exists() else None
        )
        releases = iter(collect_downloads(body).items())
        latest, latest_sources = next(releases, ('', {}))
        if 'cXML_DTDs.zip' not in latest_sources:
            raise ValueError('The downloads index did not list a current cXML archive')
        license_path = UPSTREAM / 'license.html'
        license_body = cache.get(
            LICENSE, seed=license_path.read_bytes() if license_path.exists() else None
        )
        validate(license_path.name, license_body)
        replace(license_path, license_body)
        mirror(cache, latest, latest_sources, refresh=refresh)
        replace(index, body)
        replace(UPSTREAM / 'latest.txt', (latest + '\n').encode())
        if history:
            for version, sources in releases:
                mirror(cache, version, sources, refresh=False)


def html_to_markdown(body: bytes, url: str) -> str:
    soup = BeautifulSoup(body, 'html.parser')
    content = soup.find('main') or soup.body or soup
    for tag in content.find_all(['script', 'style', 'nav', 'footer', 'iframe', 'img']):
        tag.decompose()
    for link in content.find_all('a', href=True):
        href = link.get('href')
        if isinstance(href, str) and not href.startswith('#'):
            link['href'] = urljoin(url, href)
    return markdownify(str(content), heading_style='ATX').strip()


def pdf_to_markdown(body: bytes) -> str:
    pymupdf4llm.use_layout(False)  # noqa: FBT003
    with pymupdf.open(stream=body, filetype='pdf') as document:
        text = pymupdf4llm.to_markdown(document, ignore_images=True)
    if not isinstance(text, str):
        raise TypeError('PDF converter did not return Markdown')
    return text.strip()


def write_document(path: Path, body: bytes, url: str) -> None:
    extension = path.suffix.lower()
    if extension in {'.pdf', '.html', '.htm'}:
        markdown = pdf_to_markdown(body) if extension == '.pdf' else html_to_markdown(body, url)
        if not markdown:
            raise ValueError(f'No text extracted from {path.name}')
        source = f'> Converted from the [upstream source]({url}).'
        if extension == '.pdf':
            source += ' See the original for figures and page layout.'
        replace(path.with_suffix('.md'), f'{source}\n\n{markdown}\n'.encode())
    elif extension in TEXT_EXTENSIONS:
        replace(path, body)


def extract_archive(
    output: Path, package: str, body: bytes, url: str, direct_hashes: set[str]
) -> None:
    with ZipFile(BytesIO(body)) as archive:
        for member in archive.infolist():
            name = PurePosixPath(member.filename)
            if name.is_absolute() or '..' in name.parts or search(r'[:\\]', member.filename):
                raise ValueError(f'Unsafe archive path: {member.filename}')
            extension = name.suffix.lower()
            if (
                member.is_dir()
                or extension not in TEXT_EXTENSIONS | {'.html', '.pdf'}
                or is_translated_pdf(name.name)
            ):
                continue
            original = archive.read(member)
            if extension == '.pdf' and sha256(original).hexdigest() in direct_hashes:
                continue
            if package == 'cXML_DTDs' and name.suffix == '.dtd':
                write_document(output / name.name, original, url)
            else:
                package_dir = output / 'documents' / 'packages' / package
                write_document(package_dir.joinpath(*name.parts), original, url)
                if package == 'cXML' and name.suffix in {'.dtd', '.mod'}:
                    write_document(output.joinpath(*name.parts), original, url)


def extract_zugferd(package: Path, output: Path) -> None:
    """Preserve the English release files and render the specification PDFs locally."""
    if output.exists() or output.is_symlink():
        raise ValueError(f'Extract to a new directory: {output}')

    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(package) as archive, TemporaryDirectory(prefix='zf-', dir=output.parent) as work:
        stage = Path(work) / 'extract'
        paths: set[str] = set()
        pdfs: list[Path] = []
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = PurePosixPath(member.filename)
            if (
                name.is_absolute()
                or not name.parts
                or any(
                    part.endswith(('.', ' '))
                    or search(r'[\\:*?"<>|\x00-\x1f]', part)
                    or PureWindowsPath(part).is_reserved()
                    for part in name.parts
                )
            ):
                raise ValueError(f'Unsafe archive path: {member.filename}')
            if name.parts[0] not in {'Documentation', 'Examples', 'Schema'}:
                continue
            if name.as_posix().casefold() in paths:
                raise ValueError(f'Duplicate archive path: {member.filename}')
            paths.add(name.as_posix().casefold())
            original = stage / 'originals' / name
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(archive.read(member))
            if name.parts[0] == 'Documentation' and name.suffix.lower() == '.pdf':
                pdfs.append(original)

        if not pdfs or not (stage / 'originals' / 'Schema').is_dir():
            raise ValueError('Expected Documentation PDFs and Schema files in the English release')
        markdown = stage / 'markdown'
        markdown.mkdir()
        for pdf in sorted(pdfs):
            if not (text := pdf_to_markdown(pdf.read_bytes())):
                raise ValueError(f'No text extracted from {pdf.name}')
            relative = pdf.relative_to(stage).as_posix().replace(' ', '%20')
            (markdown / pdf.with_suffix('.md').name).write_bytes(
                f'> Converted from the [original PDF](../{relative}).\n\n{text}\n'.encode()
            )
        stage.rename(output)


def collect_changelog(version: str) -> list[str]:
    latest = UPSTREAM / 'latest.txt'
    if not latest.exists():
        return []
    notes = UPSTREAM / latest.read_text(encoding='utf-8').strip()
    source = release_files(notes).get(RELEASE_NOTES)
    if source is None:
        return []
    body = (notes / RELEASE_NOTES).read_bytes()
    if sha256(body).hexdigest() != source['sha256']:
        raise ValueError(f'Source hash mismatch for {RELEASE_NOTES}')
    target = tuple(map(int, version.split('.')))
    url = str(source['url'])
    sections = split(
        r'(?m)^(?=## \d+\.\d+\.\d+[ \t]*$)',
        sub(r'(?m)^#### .* \[Back to Top\]\(#top\)$', '', html_to_markdown(body, url)),
    )
    sections = [
        section.strip()
        for section in sections[1:]
        if tuple(map(int, section.splitlines()[0][3:].strip().split('.'))) <= target
    ]
    return [f'> From the [upstream release notes]({url}).', *sections] if sections else []


def write_changelog(version: str, output: Path) -> None:
    sections = collect_changelog(version)
    if len(sections) < 2 or sections[1].splitlines()[0] != f'## {version}':
        sections.insert(0, f'The cached upstream changelog has no entry for cXML {version}.')
    replace(output / 'CHANGELOG.md', ('\n\n'.join(('# cXML changelog', *sections)) + '\n').encode())


def render(version: str, output: Path) -> None:
    if fullmatch(r'\d+\.\d+\.\d+', version) is None:
        raise ValueError(f'Invalid cXML version: {version}')
    directory = UPSTREAM / version
    records = {
        name: record
        for name, record in release_files(directory).items()
        if not is_translated_pdf(name)
    }
    if not records:
        raise ValueError(f'No original files cached for cXML {version}')
    if output.exists():
        raise ValueError(f'Render to a new directory: {output}')
    originals = {name: (directory / name).read_bytes() for name in records}
    for name, body in originals.items():
        if sha256(body).hexdigest() != records[name]['sha256']:
            raise ValueError(f'Source hash mismatch: {directory / name}')
    direct_hashes = {
        str(record['sha256']) for name, record in records.items() if name.endswith('.pdf')
    }
    documents = output / 'documents'
    write_document(output / 'LICENSE.html', (UPSTREAM / 'license.html').read_bytes(), LICENSE)
    for name, body in originals.items():
        url = str(records[name]['url'])
        if name.endswith('.zip'):
            extract_archive(output, Path(name).stem, body, url, direct_hashes)
        elif name not in {RELEASE_NOTES, RELEASE_NOTES_PAGE}:
            write_document(documents / name, body, url)
    if not (output / 'cXML.dtd').is_file():
        raise ValueError(f'No cXML.dtd found for cXML {version}')
    write_changelog(version, output)
    replace(documents / 'manifest.toml', manifest_bytes(version, records))
    replace(
        output / 'README.md',
        '\n'.join(
            (
                f'# cXML {version}',
                '',
                'Markdown reference for **cXML (commerce eXtensible Markup Language)**.',
                '',
                'English documentation and schemas from the',
                f'[official cXML downloads]({INDEX}).',
                'See [cXML on Wikipedia](https://en.wikipedia.org/wiki/CXML).',
                '',
                '## Documentation',
                '',
                *(
                    f'- [{path.stem}](documents/{path.name})'
                    for path in sorted(documents.glob('*.md'))
                ),
                '- [Packages and XML examples](documents/packages/)',
                '',
            )
        ).encode(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest='command', required=True)
    zugferd_args = commands.add_parser('extract-zugferd', help='extract an English ZUGFeRD ZIP')
    zugferd_args.add_argument('package', type=Path, help='local release ZIP')
    zugferd_args.add_argument('output', type=Path, help='new directory for the extracted files')
    pages_args = commands.add_parser('fetch-pages', help='cache the HTML pages listed in TOML')
    pages_args.add_argument('config', default=ROOT / 'sources.toml', nargs='?', type=Path)
    pages_args.add_argument('--offline', action='store_true', help='use local copies only')
    fetch_args = commands.add_parser('fetch', help='cache original cXML files')
    fetch_args.add_argument('--history', action='store_true', help='include linked older releases')
    network = fetch_args.add_mutually_exclusive_group()
    network.add_argument('--offline', action='store_true', help='use local files only')
    network.add_argument('--refresh', action='store_true', help='revalidate the current release')
    render_args = commands.add_parser(
        'render', help='convert one cached release without networking'
    )
    render_args.add_argument('version')
    render_args.add_argument('output', type=Path, help='new directory for the rendered snapshot')
    args = parser.parse_args()
    if args.command == 'extract-zugferd':
        extract_zugferd(args.package, args.output)
    elif args.command == 'fetch-pages':
        fetch_pages(args.config, offline=args.offline)
    elif args.command == 'render':
        render(args.version, args.output)
    else:
        fetch(history=args.history, offline=args.offline, refresh=args.refresh)


if __name__ == '__main__':
    try:
        main()
    except (BadZipFile, OSError, TypeError, ValueError, sqlite3.Error) as error:
        sys.exit(str(error))
