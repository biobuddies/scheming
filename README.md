# Scheming

Tools for fetching and extracting specifications.

## cXML

The public [cXML repository](https://github.com/biobuddies/cxml) has the Markdown reference for
cXML (commerce eXtensible Markup Language), including the guides, schemas, and examples.

## ZUGFeRD / Factur-X

ZUGFeRD 2.5.2 and Factur-X 1.09.2 share a specification. Download the English information package
from [FeRD](https://www.ferd-net.de/en/downloads/publications/details/zugferd-252-english).
[FNFE-MPE](https://fnfe-mpe.org/factur-x/factur-x_en/) also distributes the package.

Save the ZIP under `untracked/`, then run from this checkout with Python 3.12+ and uv:

```sh
uv run fetch_format.py extract-zugferd untracked/ZUGFeRD_2.5.2_EN.zip untracked/zugferd-2.5.2
```

Use a new output directory. The extractor preserves the publisher's documentation, schemas, and
examples under `originals/`. PyMuPDF4LLM converts the specification and profile appendices under
`markdown/`. The publisher PDFs remain the reference for figures and layout.
[FeRD usage terms](https://www.ferd-net.de/ueber-uns/ressourcen-1/veroeffentlichungen/haftungsausschluss-und-nutzungsrechte).

## EN 16931

EN 16931 defines the European e-invoicing data model and business rules. The
[European Commission's access guide](https://ec.europa.eu/digital-building-blocks/sites/spaces/DIGITAL/pages/467108971/Obtaining%2Ba%2Bcopy%2Bof%2Bthe%2BEuropean%2Bstandard%2Bon%2BeInvoicing)
points to the normative documents. [ConnectingEurope](https://github.com/ConnectingEurope/eInvoicing-EN16931)
publishes the validation rules and example XML.

## Cache publisher pages

`uv run fetch_format.py fetch-pages` reads the URLs in `sources.toml` and saves the full responses
in SQLite and `untracked/pages/`. A repeat run reuses those responses. Pass `--offline` to
require cached copies. The URL's last path component supplies the filename unless a source
specifies a `filename` override.
