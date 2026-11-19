// Every kind of port the boundary register handles, for the full-BSDL checks: a clock and a
// reset left off the register, scalar and multi-bit inputs (one declared [lo:hi]), scalar and
// multi-bit outputs, and scalar and multi-bit bidirectional pins with their own enables.
module bsr_buses (
    input        clk,
    input        rst_n,
    input        a,
    input  [3:0] din,
    input  [0:1] lane,
    input        io_oe,
    input        dbus_oe,
    output [2:0] dout,
    output       flag,
    inout        io,
    inout  [1:0] dbus
);
    reg [2:0] q;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) q <= 3'b0;
        else q <= din[2:0] ^ {lane, a};
    assign dout = q;
    assign flag = ^din;
    assign io = io_oe ? a : 1'bz;
    assign dbus = dbus_oe ? din[1:0] : 2'bzz;
endmodule
