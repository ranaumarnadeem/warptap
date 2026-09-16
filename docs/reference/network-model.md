# Network Model

The physical shift-topology graph and its node types — see also [Nested SIB
networks](../guide/nested-sib-networks.md) and [Multi-arm ScanMux](../guide/scan-mux.md) for
how these compose.

::: warptap.icl_model
    options:
      members:
        - PhysicalGraph
        - SibNode
        - ScanMuxNode
        - ScanArm
        - InstrumentNode
        - InstrumentDirection
        - SignalBinding
        - Alias
        - ModuleInstance
        - resolve_dotted_address
        - ICLAddressError
