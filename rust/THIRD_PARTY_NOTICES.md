# Third-party notices

`english-core/src/fsrs_compat.rs` adapts the scheduler math and default weights
from py-fsrs 6.3.0 (https://github.com/open-spaced-repetition/py-fsrs).
The frozen compatibility scope uses empty learning/relearning steps, no fuzz,
and the app's 365-day maximum. Application study transactions remain separate.

MIT License

Copyright (c) 2022 Open Spaced Repetition

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


The native binaries bundle SQLite (public domain) and OpenSSL 3.6.3.
OpenSSL is licensed under Apache License 2.0; see `licenses/OpenSSL-Apache-2.0.txt`.
Other Rust dependencies and exact versions are recorded in Cargo.lock.
