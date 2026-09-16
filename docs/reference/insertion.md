# Insertion

Building a network plan, then actually inserting it into a netlist.

::: warptap.sib_plan
    options:
      members: [HierarchySpec, InstrumentSpec, build_sib_plan]

::: warptap.sib_insert
    options:
      members: [SibInsertError, insert_sib_network]

::: warptap.bsr_insert
    options:
      members: [BsrInsertError, insert_bsr]

::: warptap.pipeline
    options:
      members: [insert_test_access]
