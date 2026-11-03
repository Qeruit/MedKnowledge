The adjacent binary file is the public `cl100k_base.tiktoken` vocabulary used
by `tiktoken`. Upstream URL:
`https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken`.

Its SHA-256 is
`223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7`,
which is the digest expected by the installed `tiktoken` loader. The Docker
build verifies this hash and preloads the vocabulary so that runtime chat and
indexing never depend on a first-use download from that host.
