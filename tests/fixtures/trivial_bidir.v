// Small fixture exercising the highest-uncertainty piece of Stage 3's design (implementation_
// plan.md §7): a real `inout` port via the standard `assign io = oe ? val : 1'bz;` idiom,
// which Yosys lowers to a $tribuf cell -- bsr_insert.py's bidir path must find and reuse that
// existing $tribuf's A/EN connections rather than guessing.
module trivial_bidir (
    input  wire clk,
    input  wire rst_n,
    input  wire oe,      // output-enable for the bidirectional pin
    input  wire val,     // value to drive when oe=1
    inout  wire io,      // the bidirectional pin
    output wire sensed   // registered observation of io's live state
);
    reg sensed_r;

    assign io = oe ? val : 1'bz;

    always @(posedge clk or negedge rst_n)
        if (!rst_n)
            sensed_r <= 1'b0;
        else
            sensed_r <= io;

    assign sensed = sensed_r;
endmodule
