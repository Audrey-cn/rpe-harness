from .cli import main
from .path_binding import inherited_binding

with inherited_binding():
    raise SystemExit(main())
