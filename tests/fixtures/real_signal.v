// Fixture for the generic write/read functional-instrument cross-sim (implementation_plan.md
// §7 Stage 9 §5.1). Deliberately small but Python-predictable: `ctrl_in` is what the new
// `instrument_write` cell will drive (after sib_insert.py detaches it), `ctrl_latched` mirrors
// it one clock later, `status_out` is what a `bc1_shift_only` READ cell observes.
module real_signal (
    input  wire clk,
    input  wire rst_n,
    input  wire ctrl_in,
    output wire status_out
);
    reg ctrl_latched;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            ctrl_latched <= 1'b0;
        else
            ctrl_latched <= ctrl_in;
    end

    assign status_out = ctrl_latched;
endmodule
