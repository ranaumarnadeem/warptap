// Fixture for the ScanMux-arm WRITE-readback RTL spike (mux-arm readback bug fix plan, Phase
// 0). A wider (multi-bit) sibling of real_signal.v, kept as its own separate file rather than
// widening real_signal.v itself -- real_signal.v is shared by several already-passing tests
// (test_sib_insert_write_instrument_cross_sim.py, test_sib_insert_scan_mux_cross_sim.py,
// test_pdl_interpreter_cross_sim.py, test_scan_mux_deep_integration.py) that all assume its
// existing 1-bit ctrl_in/status_out shape; this spike specifically needs a multi-bit host port
// so a committed value's own bit pattern (e.g. 0b101) is actually distinguishable from a
// single stuck bit, which a 1-bit port structurally cannot demonstrate.
module wide_signal (
    input  wire clk,
    input  wire rst_n,
    input  wire [2:0] ctrl_in,
    input  wire [1:0] ctrl2_in,
    output wire [2:0] status_out
);
    reg [2:0] ctrl_latched;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            ctrl_latched <= 3'b0;
        else
            ctrl_latched <= ctrl_in;
    end

    assign status_out = ctrl_latched;
endmodule
