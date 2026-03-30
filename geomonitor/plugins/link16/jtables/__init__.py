"""J-Series message table registry.

Each module in this package defines field layouts for a group of J-Series
messages and registers them via register_message(). Import register_all()
to load every available table.

Adding support for a new message type:
1. Create or edit a module in this package (e.g. j2_ppli.py)
2. Define a MessageDef with FieldDef entries for each field
3. Call register_message(msg_def) at module level
4. Add the module import to register_all() below

The FieldDef positions reference the 70-bit J-word block after byte-swap:
  Word 0 (Initial): bits  0-15  (contains header: WF[0:2], Label[2:7], SubLabel[7:10])
  Word 1:           bits 16-31
  Word 2:           bits 32-47
  Word 3:           bits 48-63
  Word 4:           bits 64-69  (6 bits only)
"""


def register_all() -> None:
    """Import all table modules, triggering their register_message() calls."""
    from geomonitor.plugins.link16.jtables import j2_ppli  # noqa: F401
    from geomonitor.plugins.link16.jtables import j3_surveillance  # noqa: F401
