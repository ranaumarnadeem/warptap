// Fixture for the STIL replay tests of multi-bit pins and Loop blocks: every clk pulse adds
// the 4-bit input `step` to an 8-bit count, so reading `count_out` back tells how many pulses
// ran and which value each `step` bit carried.
module stil_counter (
    input  wire clk,
    input  wire rst_n,
    input  wire [3:0] step,
    output wire [7:0] count_out
);
    reg [7:0] count;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            count <= 8'd0;
        else
            count <= count + {4'd0, step};
    end

    assign count_out = count;
endmodule
