// Plain functional testbench for the UNMODIFIED `trivial` fixture (implementation_plan.md §7
// Stage 3's functional-equivalence proof: this is the baseline the inserted design's `y`
// output must match, cycle for cycle, whenever trst_n is held asserted-low).
//
// Stimulus file: one line of two integers each: "<a> <b>" (rst_n is driven separately, see
// initial block). Samples `y` (pre-edge) then $displays a sentinel-prefixed trace line
// before applying the clock edge.
`timescale 1ns/1ps

module tb_trivial_functional;
    reg clk = 0;
    reg rst_n = 1;
    reg a = 0;
    reg b = 0;
    wire y;

    trivial dut (
        .clk(clk),
        .rst_n(rst_n),
        .a(a),
        .b(b),
        .y(y)
    );

    integer fd;
    integer code;
    integer rst_n_in, a_in, b_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d", rst_n_in, a_in, b_in);
            if (code != 3) begin
                done = 1;
            end else begin
                rst_n = rst_n_in[0];
                a = a_in[0];
                b = b_in[0];
                #1;
                $display("TRACE,%0d", y);
                clk = 1;
                #1;
                clk = 0;
                #1;
            end
        end

        $fclose(fd);
        $finish;
    end
endmodule
