// Boundary-scan cell, input/internal function (implementation_plan.md §7 Stage 3): the
// observe-only half of BC_1's shape. A cell driving nothing externally has nothing for an
// update latch to hold between Update-DR pulses, so this cell is just capture + shift.
//
// capture_dr/shift_dr are tap_core's own per-state strobes (one-cycle, gated on the TAP
// being resident in that state, not free-running) -- this module never re-derives them.
module bc1_shift_only (
    input  wire pi,          // parallel input: the pin/net this cell observes
    input  wire si,          // serial input, chained from the previous cell (toward TDI)
    output wire so,          // serial output, chained to the next cell (toward TDO)
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire tck,
    input  wire trst_n
);
    reg shift_ff;

    // Resets to 0, matching tap_core.v's own ir_shift reset discipline -- avoids
    // X-propagation in simulation before the first real Capture-DR/Shift-DR cycle.
    always @(posedge tck or negedge trst_n) begin
        if (!trst_n)
            shift_ff <= 1'b0;
        else if (capture_dr)
            shift_ff <= pi;
        else if (shift_dr)
            shift_ff <= si;
    end

    assign so = shift_ff;
endmodule
