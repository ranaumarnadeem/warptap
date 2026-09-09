// N-arm ScanMux cell (multi-arm ScanMux plan, Phase 0/5): the IEEE 1687 primitive that
// conditionally splices exactly ONE of ARMS mutually-exclusive nested segments into the
// active scan chain, chosen by an SEL_WIDTH-bit self-select register -- the N-arm
// generalization of sib_cell.v's binary (1-bit, 2-outcome) open/closed splice. A new,
// separate primitive rather than a sib_cell.v extension: sib_cell's shift_ff/po are
// single-bit by construction (the whole point of `po ? nested_so : si`), and bolting a
// decode loop onto a just-shipped, cross-sim-validated file risks destabilizing the common
// 2-arm case for no benefit to it.
//
// Structurally: `shift_ff`/`po` generalize from a single bit to an SEL_WIDTH-bit shift
// register (the self-select field), mirroring tap_core.v's own `ir_shift` (MSB-in/LSB-out
// right shift) -- but ONLY while no arm is currently matched (bypass). Once `po` (the OLD,
// already-committed value from a prior Update-DR) decodes to some arm k, this cell's own
// select field is fed from that arm's `arm_so[k]` every Shift-DR cycle instead of `si` --
// exactly sib_cell.v's own `po ? nested_so : si`, generalized bit-by-bit (and at
// SEL_WIDTH=1, ARMS=2, this reduces to that exact expression). Because a mux slot's bit
// layout places the select field's own SEL_WIDTH bits first in a round (TDI-farthest -- fed
// earliest) and the matched arm's real content last (TDI-nearest -- fed most recently, right
// before Update-DR), a retargeting round's fresh select-field bits reach `shift_ff` correctly
// by relaying THROUGH the currently-matched arm's own register with the same 1-cycle delay
// sib_cell.v's `nested_so` relay already relies on -- confirmed by hand-tracing a 2-bit,
// 1-arm close-while-relaying-dummy-content round bit-for-bit before writing this file, the
// same discipline the nested-SIB plan's own Phase 0 spike established. Cross-sim-verified
// empirically by tests/test_scan_mux_cell_cross_sim.py, not just reasoned here.
//
// `so` (this cell's own contribution to whatever's next in the outer chain, toward TDO) is
// NOT simply `shift_ff[0]` unconditionally the way a plain SEL_WIDTH-bit shift register
// (ir_shift) would read out -- that would introduce up to SEL_WIDTH-1 cycles of extra,
// un-accounted-for latency once matched (the internal register's own queueing depth),
// breaking the exact TDI-nearest-first bit-count arithmetic every other part of this network
// relies on. Instead `so` is a combinational choice: the matched arm's own `arm_so[k]`
// directly (zero extra delay, exactly mirroring sib_cell.v's `nested_so` relay) while
// matched, or this cell's own register's LSB (the plain-shift-register readout) while
// bypassing.
//
// Decode/shift logic uses `generate`/`genvar` and reduction operators, NOT a `for` loop
// inside an `always` block (an earlier version did, and passed its own direct-iverilog
// cross-sim cleanly, since Phase 0's spike never round-tripped through Yosys) -- found, not
// assumed: inserting this cell via sib_insert.py (which ingests every RTL template through
// Yosys `read_verilog`/`write_verilog` to fold it into one netlist, unlike Phase 0's spike,
// which hands rtl files directly to iverilog) produced genuinely broken output for a
// `for`-loop-in-`always` integer variable (`assign 32'd2 = <signal>;` -- operands backwards,
// a real Yosys `write_verilog` round-trip limitation for that construct, reproduced and
// confirmed via a standalone ingest+write_verilog+iverilog-compile check before rewriting).
// `generate`/`genvar` and `generate if` (for the SEL_WIDTH==1 degenerate case, which a plain
// `{fresh_bit, shift_ff[SEL_WIDTH-1:1]}` concatenation can't express -- see sib_cell.v's own
// note on the same edge case) are both standard, widely-synthesized constructs and round-trip
// cleanly. Re-verified bit-for-bit equivalent to the original loop-based version via this
// same file's own cross-sim test before and after the rewrite.
//
// V1 requires each arm to have exactly one committed value (icl_model.ScanArm's own
// `values` field is forward-compatible with a real multi-value catch-all arm, but every
// consuming code path -- this one included -- assumes ARM_VALUES holds exactly one packed
// value per arm slot; see icl_model.py's own module docstring).
module scan_mux_cell #(
    parameter ARMS = 2,
    parameter SEL_WIDTH = 1,
    parameter [ARMS*SEL_WIDTH-1:0] ARM_VALUES = 0
) (
    input  wire            si,             // serial input, chained from the previous element
    output wire            so,              // this cell's own live select-field content, or
                                              // (while matched) the matched arm's relayed so
    input  wire [ARMS-1:0] arm_so,          // scan-out fed back from each of the ARMS arms
    output wire            nested_si,       // = si, fanned to every arm unconditionally
    output wire [ARMS-1:0] arm_select,      // per-arm nested_select equivalent: stays-matched
                                              // across this edge (old po AND new shift_ff)
    output wire [ARMS-1:0] arm_active,      // per-arm nested_active equivalent: matched
                                              // before this edge, for the whole open window
    input  wire            select,
    input  wire            capture_dr,
    input  wire            shift_dr,
    input  wire            update_dr,
    input  wire            tck,
    input  wire            trst_n
);
    reg [SEL_WIDTH-1:0] shift_ff, po;   // resets to 0 -- value 0 is never a modeled arm (an
                                          // unmatched po is always implicit bypass), the same
                                          // hardware-guaranteed-safe-default sib_cell.v's own
                                          // po=0-means-closed already relies on.

    // arm_match_old[k]/arm_match_new[k]: does po/shift_ff decode to arm k. At most one bit of
    // each is ever set (icl_model.validate_physical_graph guarantees every arm's value is
    // unique within one mux), so a plain OR-reduction below correctly picks "the" match.
    wire [ARMS-1:0] arm_match_old, arm_match_new;
    genvar g;
    generate
        for (g = 0; g < ARMS; g = g + 1) begin : DECODE
            assign arm_match_old[g] = (po == ARM_VALUES[g*SEL_WIDTH +: SEL_WIDTH]);
            assign arm_match_new[g] = (shift_ff == ARM_VALUES[g*SEL_WIDTH +: SEL_WIDTH]);
            assign arm_active[g] = arm_match_old[g] & select;
            assign arm_select[g] = arm_match_old[g] & arm_match_new[g] & select;
        end
    endgenerate

    wire matched_old_c = |arm_match_old;               // po decodes to some arm
    wire relay_so_c = |(arm_match_old & arm_so);        // that arm's own arm_so bit (0 if none)

    generate
        if (SEL_WIDTH == 1) begin : SEL_SCALAR
            // The concatenation form below needs SEL_WIDTH >= 2 (shift_ff[SEL_WIDTH-1:1]
            // would otherwise be the invalid, backwards range [0:1]) -- this degenerates to
            // exactly sib_cell.v's own `shift_ff <= po ? nested_so : si`.
            always @(posedge tck or negedge trst_n) begin
                if (!trst_n) begin
                    shift_ff <= 1'b0;
                    po <= 1'b0;
                end else if (select) begin
                    if (capture_dr)
                        shift_ff <= po;
                    else if (shift_dr)
                        shift_ff <= matched_old_c ? relay_so_c : si;
                    if (update_dr)
                        po <= shift_ff;
                end
            end
        end else begin : SEL_VECTOR
            always @(posedge tck or negedge trst_n) begin
                if (!trst_n) begin
                    shift_ff <= {SEL_WIDTH{1'b0}};
                    po <= {SEL_WIDTH{1'b0}};
                end else if (select) begin
                    if (capture_dr) begin
                        shift_ff <= po;                     // self-capture, mirrors sib_cell.v
                    end else if (shift_dr) begin
                        shift_ff <= matched_old_c
                            ? {relay_so_c, shift_ff[SEL_WIDTH-1:1]}
                            : {si, shift_ff[SEL_WIDTH-1:1]};
                    end
                    if (update_dr)
                        po <= shift_ff;
                end
            end
        end
    endgenerate

    assign so = matched_old_c ? relay_so_c : shift_ff[0];
    assign nested_si = si;
endmodule
