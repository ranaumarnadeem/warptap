// Segment Insertion Bit (implementation_plan.md §7 Stage 4): the IEEE 1687 primitive that
// conditionally splices a nested instrument segment into the active scan chain. Mirrors
// bc1_full.v's exact shift_ff/po shape (self-capture, second-rank update latch), plus the one
// mechanism BC_1 never needed: `select`/`nested_select`, since a SIB's segment is only
// conditionally part of the active chain, while a boundary-scan cell never is.
//
// Signal-for-signal shape confirmed against koopahi/Secure-IJTAG's SIB.v and cross-checked
// against the IEEE 1687 SIB-Gateway literature description (this project's own naming
// convention throughout, not koopahi's -- e.g. koopahi's UpdateEn/UpdateEN case-mismatch bug
// is not replicated here).
module sib_cell (
    input  wire si,             // serial input, chained from the previous network element
    output wire so,              // this SIB's own shift_ff, open or closed
    input  wire nested_so,       // scan-out fed back from the nested segment this SIB gates
    output wire nested_si,       // = si, unconditionally
    output wire nested_select,   // = po & select -- exposed for future nested-SIB use;
                                  // v1 never wires it to anything that consumes it
    input  wire select,          // AND-ed into every local action; v1 ties this to the
                                  // constant 1 for every top-level SIB (unconditionally
                                  // reachable directly off the TAP)
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire update_dr,
    input  wire tck,
    input  wire trst_n
);
    reg shift_ff;
    reg po;   // 1 = open (nested segment spliced in), 0 = closed (bypass) -- resets to 0,
              // same hardware-guaranteed-safe-default property bc1_full.v's po already has.

    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            shift_ff <= 1'b0;
            po <= 1'b0;
        end else if (select) begin
            if (capture_dr)
                shift_ff <= po;                  // self-capture: own open/closed state,
                                                   // a real design choice for network-state
                                                   // diagnostics, not a hardwired 0
            else if (shift_dr)
                shift_ff <= po ? nested_so : si;  // scan-splice: open routes through the
                                                   // nested segment, closed bypasses it
            if (update_dr)
                po <= shift_ff;
        end
    end

    assign so = shift_ff;
    assign nested_si = si;
    assign nested_select = po & select;
endmodule
