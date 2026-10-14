# Third-party notices

`src/rpe_harness/assets/identity.py` selectively adapts the canonical JSON,
public-key fingerprint, and RSA PKCS#1 v1.5 SHA256 signing/verification code
from [Audrey-cn/progenitor-protocol](https://github.com/Audrey-cn/progenitor-protocol),
`hatchery/stargate_identity.py`, pinned to commit
`84bc86e121aac5c71a53abc23189cc07e562b256`.

The port adds bounded input validation and explicit purpose-bound documents and
local policy. It does not import the original runtime, global identities,
key generation, or dynamic signature-scheme registration.

## MIT License

Copyright (c) 2026 Audrey (Audrey-cn)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
