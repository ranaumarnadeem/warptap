// Testbench for a SIB-network-inserted `mem_subsystem_mbist` module (implementation_plan.md
// §7 Stage 9 §5.2) -- the real openMBIST target, not a warptap fixture. Mirrors
// tb_sib_trivial.v's shape and stimulus-file protocol exactly -- clk and tck driven from the
// SAME physical clock (lockstep). test_mode/bist_start/self_repair_start are tied to constant
// 0 here (implementation_plan.md's own documented, intentional consequence: after insertion
// their *new* top-level port bits are legal but functionally vestigial -- JTAG is the only
// path in); bist_done/bist_fail/self_repair_done/self_repair_fail/self_repair_busy are left
// unconnected (READ instruments observe their host bits directly, no testbench wiring needed).
//
// Stimulus file: one line of nine integers each:
// "<tms> <tdi> <trst_n> <rst_n> <csb> <we> <mem_sel> <addr> <wdata>".
`timescale 1ns/1ps

module tb_mem_subsystem_mbist;
    reg clk = 0;
    reg tms = 0;
    reg tdi = 0;
    reg trst_n = 1;
    reg rst_n = 1;
    reg csb = 1;
    reg we = 0;
    reg [1:0] mem_sel = 0;
    reg [9:0] addr = 0;
    reg [31:0] wdata = 0;
    wire tdo;
    wire [31:0] rdata;

    mem_subsystem_mbist dut (
        .clk(clk),
        .rst_n(rst_n),
        .csb(csb),
        .we(we),
        .mem_sel(mem_sel),
        .addr(addr),
        .wdata(wdata),
        .rdata(rdata),
        .test_mode(1'b0),
        .bist_start(1'b0),
        .bist_done(),
        .bist_fail(),
        .self_repair_start(1'b0),
        .self_repair_done(),
        .self_repair_fail(),
        .self_repair_busy(),
        .tck(clk),
        .tms(tms),
        .tdi(tdi),
        .trst_n(trst_n),
        .tdo(tdo)
    );

    integer fd;
    integer code;
    integer tms_in, tdi_in, trst_n_in, rst_n_in, csb_in, we_in, mem_sel_in, addr_in, wdata_in;
    reg done;

    initial begin
        done = 0;
        fd = $fopen("stimulus.txt", "r");
        if (fd == 0) begin
            $display("ERROR: could not open stimulus.txt");
            $finish;
        end

        while (!done) begin
            code = $fscanf(fd, "%d %d %d %d %d %d %d %d %d",
                            tms_in, tdi_in, trst_n_in, rst_n_in, csb_in, we_in, mem_sel_in,
                            addr_in, wdata_in);
            if (code != 9) begin
                done = 1;
            end else begin
                tms = tms_in[0];
                tdi = tdi_in[0];
                trst_n = trst_n_in[0];
                rst_n = rst_n_in[0];
                csb = csb_in[0];
                we = we_in[0];
                mem_sel = mem_sel_in[1:0];
                addr = addr_in[9:0];
                wdata = wdata_in[31:0];
                #1; // let combinational logic settle before sampling
                $display("TRACE,%0d,%0d,%0d,%0d,%0d,%0d,%0d",
                          dut.warptap_tap_state, dut.warptap_current_instruction,
                          dut.warptap_capture_dr, dut.warptap_shift_dr, dut.warptap_update_dr,
                          tdo, rdata);
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
