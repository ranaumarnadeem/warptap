// Plain functional testbench for the UNMODIFIED `trivial_bidir` fixture (implementation_plan.
// md §7 Stage 3's bidir increment) -- the baseline the inserted design's `sensed` output must
// match under normal (non-EXTEST) operation, proving the tri-state-driver-discovery/reconnect
// mechanism preserves original behavior.
//
// Stimulus file: one line of two integers each: "<oe> <val>" (rst_n held high throughout;
// `io` is a genuine wire, driven only by the DUT itself, no external contention modeled).
`timescale 1ns/1ps

module tb_trivial_bidir_functional;
    reg clk = 0;
    reg rst_n = 1;
    reg oe = 0;
    reg val = 0;
    wire io;
    wire sensed;

    trivial_bidir dut (
        .clk(clk),
        .rst_n(rst_n),
        .oe(oe),
        .val(val),
        .io(io),
        .sensed(sensed)
    );

    integer fd;
    integer code;
    integer oe_in, val_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d", oe_in, val_in);
            if (code != 2) begin
                done = 1;
            end else begin
                oe = oe_in[0];
                val = val_in[0];
                #1;
                $display("TRACE,%0d,%0d", sensed, io);
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
