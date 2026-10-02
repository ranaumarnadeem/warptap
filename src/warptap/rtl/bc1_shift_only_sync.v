// bc1_shift_only with a two-flop synchronizer in front of its capture: the observe-only
// cell for a pin driven from another clock domain (a READ instrument with capture_sync).
// The pin passes through two TCK flops before Capture-DR samples it, so a capture shows
// the pin as it was two TCK edges earlier, never a value caught mid-transition. Both flops
// reset with trst_n, like the shift flop.
//
// Each bit has its own synchronizer: a multi-bit value is coherent only if it holds still
// for those two edges -- right for a status that settles (an MBIST's done/fail), wrong for
// a counter that keeps moving.
module bc1_shift_only_sync (
    input  wire pi,          // parallel input: the pin/net this cell observes
    input  wire si,          // serial input, chained from the previous cell (toward TDI)
    output wire so,          // serial output, chained to the next cell (toward TDO)
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire tck,
    input  wire trst_n
);
    reg sync_first;
    reg sync_second;
    reg shift_ff;

    always @(posedge tck or negedge trst_n) begin
        if (!trst_n) begin
            sync_first <= 1'b0;
            sync_second <= 1'b0;
        end else begin
            sync_first <= pi;
            sync_second <= sync_first;
        end
    end

    always @(posedge tck or negedge trst_n) begin
        if (!trst_n)
            shift_ff <= 1'b0;
        else if (capture_dr)
            shift_ff <= sync_second;
        else if (shift_dr)
            shift_ff <= si;
    end

    assign so = shift_ff;
endmodule
