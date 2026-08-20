// IJTAG instrument write cell (implementation_plan.md §7 Stage 9): the write-target half of a
// real (non-stub) functional TDR, giving PDL genuine read/write access to an external design
// signal instead of a fixed capture_value. Structurally bc1_shift_only.v's capture/shift shape
// plus sib_cell.v's own po update-latch.
//
// pin_out (= po) is driven unconditionally and permanently once committed -- no
// extest_mode-equivalent mux, no functional passthrough input. A pure JTAG-instrument control
// signal has no competing "normal mode" driver the way a boundary-scan pin cell does
// (bc1_full.v's whole reason for a mux): confirmed by contrasting real designs --
// theuppercaseguy/ijtag-rtl's boundary-scan bsc.sv muxes a competing sys_in, while
// TuanDuc44/IHP130-DFT-Memory-Subsystem's pure MBIST control signal is driven with no mux at
// all (`assign mbist_start = (wir == MBIST);`).
//
// `select` (wired to the gating SIB's own `nested_select` -- sib_cell.v's own "exposed for
// future nested-SIB use; v1 never wires it to anything" output, now consumed) gates ONLY the
// update-latch commit, not capture_dr/shift_dr: unlike bc1_shift_only.v (no persistent state,
// so garbage-shifting through a closed instrument is harmless -- pdl_interpreter.py's own
// iApply docstring), this cell's `po` drives a REAL host signal permanently. Without this
// gate, ANY iApply's phase-1 retargeting shift -- which unconditionally walks capture_dr/
// shift_dr/update_dr through every instrument cell in the network, open or not, exactly like
// every other v1 cell -- would commit whatever garbage bits happened to shift through this
// cell's shift_ff into its real external pin, even while a completely different instrument is
// being targeted -- including a *closing* transition: the very edge that flips the gating
// SIB's own `po` from 1 to 0 also carries whatever "don't care" content phase 1 fed this
// instrument, so gating on `po` alone would still let a retarget *away* from an already-open
// WRITE instrument clobber it. `select` (= `po & shift_ff`, sib_cell.v's `nested_select`) is
// true only while the gating SIB is open both before *and* after this edge, so an instrument
// is write-committable only on an edge that leaves it open -- never one that opens or closes
// it.
//
// Capture-DR self-captures po (mirrors sib_cell.v's own self-capture): reading a write
// instrument reads back whatever was last committed, for free.
module instrument_write (
    input  wire si,           // serial input, chained from the previous cell (toward TDI)
    output wire so,            // serial output, chained to the next cell (toward TDO)
    output wire pin_out,       // = po, unconditionally -- drives the real host signal
    input  wire select,        // = gating SIB's nested_select; gates the update-latch commit
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire update_dr,
    input  wire tck,
    input  wire trst_n
);
    reg shift_ff;
    reg po;   // resets to 0 -- every real target signal this cell has driven so far
              // (test_mode, bist_start, self_repair_start) is a real hardware guarantee to
              // come up de-asserted before any PDL has run.

    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            shift_ff <= 1'b0;
            po <= 1'b0;
        end else begin
            if (capture_dr)
                shift_ff <= po;      // self-capture: read back the last committed write
            else if (shift_dr)
                shift_ff <= si;
            if (update_dr && select)
                po <= shift_ff;
        end
    end

    assign so = shift_ff;
    assign pin_out = po;
endmodule
