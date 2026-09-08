// N-arm ScanMux cell (multi-arm ScanMux plan, Phase 0): the IEEE 1687 primitive that
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
    output reg  [ARMS-1:0] arm_select,      // per-arm nested_select equivalent: stays-matched
                                              // across this edge (old po AND new shift_ff)
    output reg  [ARMS-1:0] arm_active,      // per-arm nested_active equivalent: matched
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

    integer i;
    reg matched_old_c;      // po (OLD, this edge) decodes to some arm
    reg relay_so_c;         // that arm's own arm_so bit, for the shift-relay and `so` output

    always @(*) begin
        matched_old_c = 1'b0;
        relay_so_c = 1'b0;
        arm_active = {ARMS{1'b0}};
        arm_select = {ARMS{1'b0}};
        for (i = 0; i < ARMS; i = i + 1) begin
            if (po == ARM_VALUES[i*SEL_WIDTH +: SEL_WIDTH]) begin
                matched_old_c = 1'b1;
                relay_so_c = arm_so[i];
                arm_active[i] = select;
                arm_select[i] = select
                    && (shift_ff == ARM_VALUES[i*SEL_WIDTH +: SEL_WIDTH]);
            end
        end
    end

    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            shift_ff <= {SEL_WIDTH{1'b0}};
            po <= {SEL_WIDTH{1'b0}};
        end else if (select) begin
            if (capture_dr) begin
                shift_ff <= po;                     // self-capture, mirrors sib_cell.v
            end else if (shift_dr) begin
                for (i = 0; i < SEL_WIDTH - 1; i = i + 1)
                    shift_ff[i] <= shift_ff[i + 1];  // right-shift the rest down
                shift_ff[SEL_WIDTH - 1] <= matched_old_c ? relay_so_c : si;  // fresh bit in
            end
            if (update_dr)
                po <= shift_ff;
        end
    end

    assign so = matched_old_c ? relay_so_c : shift_ff[0];
    assign nested_si = si;
endmodule
