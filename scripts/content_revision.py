"""Print the content revision of this checkout.

A validator reports the same digest as `content_revision` on `/health`. Running
this from a checkout of the commit an image was built from must produce the
identical value; a mismatch means the deployment is not running that source.

    python -m scripts.content_revision
"""

from __future__ import annotations

from endure.runtime.identity import content_revision


def main() -> None:
    print(content_revision())


if __name__ == "__main__":
    main()
