// Trivial fixture design for Stage 1's pipeline skeleton (implementation_plan.md §7,
// Stage 1). Deliberately small but non-degenerate: an `always` block (exercises
// `proc` lowering of behavioral processes, which `write_json` requires) plus a
// combinational gate feeding a registered output.
module trivial (
    input  wire clk,
    input  wire rst_n,
    input  wire a,
    input  wire b,
    output wire y
);
    reg q;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            q <= 1'b0;
        else
            q <= a & b;
    end

    assign y = q;
endmodule
