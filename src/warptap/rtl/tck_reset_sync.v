// The control TDRs' chip-reset clear, released on TCK (insert_sib_network's chip_reset).
// clr_n asserts as soon as trst_n or the chip reset does, with no clock, and releases two
// TCK edges after both have: the chip reset comes from another clock domain, and releasing
// a flop's clear asynchronously near a TCK edge could leave it metastable. With TCK stopped,
// clr_n stays low.
module tck_reset_sync (
    input  wire tck,
    input  wire trst_n,
    input  wire chip_rst_n,  // the chip reset, active low
    output wire clr_n        // low: the control TDRs are cleared
);
    wire clear_n = trst_n & chip_rst_n;
    reg [1:0] stages;

    always @(posedge tck or negedge clear_n) begin
        if (!clear_n)
            stages <= 2'b00;
        else
            stages <= {stages[0], 1'b1};
    end

    assign clr_n = stages[1];
endmodule
