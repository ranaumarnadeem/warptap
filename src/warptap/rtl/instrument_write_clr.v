// instrument_write with a second clear (insert_sib_network's chip_reset): clr_n low clears
// the cell exactly as trst_n does, so the signal it drives comes up deasserted after a chip
// reset even when TRST never pulsed. clr_n comes from tck_reset_sync: asserted with the chip
// reset, released on TCK. Everything else is instrument_write.v's behavior.
module instrument_write_clr (
    input  wire si,
    output wire so,
    output wire pin_out,
    input  wire select,
    input  wire capture_dr,
    input  wire shift_dr,
    input  wire update_dr,
    input  wire tck,
    input  wire trst_n,
    input  wire clr_n
);
    wire rst_n = trst_n & clr_n;
    reg shift_ff;
    reg po;

    always @(posedge tck or negedge rst_n) begin
        if (!rst_n) begin
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
