"""Tiny 2-layer test design: USB 5 V in -> AP2112K 3.3 V LDO -> LED + header.

Exercises the generic paths of the physical pipeline: estimated board size, no anchors,
no planes, two sheets, net classes, MPN/LCSC fields and a no-connect pin.
"""

from skidl import *  # noqa: F403 - NC lives in the default circuit

FP_R = "Resistor_SMD:R_0603_1608Metric"
FP_C = "Capacitor_SMD:C_0603_1608Metric"


def passive(name, value, fp, mpn, lcsc):
    p = Part("Device", name, value=value, footprint=fp)
    p.fields["MPN"] = mpn
    p.fields["LCSC"] = lcsc
    return p


@subcircuit
def regulator(vin, vout, gnd):
    u = Part("Regulator_Linear", "AP2112K-3.3", footprint="Package_TO_SOT_SMD:SOT-23-5")
    u.fields["MPN"] = "AP2112K-3.3TRG1"
    u.fields["LCSC"] = "C51118"
    u["VIN"] += vin
    u["EN"] += vin
    u["VOUT"] += vout
    u["GND"] += gnd
    u["NC"] += NC
    for net in (vin, vout):
        c = passive("C", "1uF", FP_C, "GRM188R71C105KA12D", "C15849")
        c[1, 2] += net, gnd


@subcircuit
def indicator(v, gnd, sig):
    r = passive("R", "1k", FP_R, "RC0603FR-071KL", "C21190")
    led = passive("LED", "RED", "LED_SMD:LED_0603_1608Metric", "LTST-C191KRKT", "C2286")
    r[1, 2] += v, led["A"]
    led["K"] += sig


def build():
    gnd, vin, v3 = Net("GND"), Net("+5V"), Net("+3V3")
    led_k = Net("LED_K")
    power = NetClass("Power", track_width=0.4, clearance=0.2)
    for n in (gnd, vin, v3):
        n.netclasses = power
    j = Part("Connector_Generic", "Conn_01x04",
             footprint="Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical")
    j.fields["MPN"] = "61300411121"
    j[1, 2, 3, 4] += vin, v3, led_k, gnd
    for n in (vin, gnd):  # fed from the connector: mark as driven for ERC
        Part("power", "PWR_FLAG")[1] += n
    regulator(vin, v3, gnd)
    indicator(v3, gnd, led_k)
