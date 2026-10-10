# Automatic reading - accuracy on the reference documents

Generated 10-10-2026 22:49 with OK (mode=aistudio, fast=gemini-3.8-flash, pro=gemini-3.1-pro-preview).

| Document | Correct | Total | Accuracy | Time |
|---|---|---|---|---|
| Customer request form (scanned) | 16 | 16 | 100% | 23 s |
| Proforma for transformers (scanned) | 16 | 16 | 100% | 38 s |
| Routine test logsheet (scanned) | 52 | 54 | 96% | 19 s |
| Short-circuit logsheet (scanned) | 38 | 38 | 100% | 27 s |
| Losses workbook, foreign layout (Excel) | 68 | 68 | 100% | 31 s |
| **All documents** | **190** | **192** | **99%** | |

## Customer request form (scanned)

| Field | Read | Confidence | Result |
|---|---|---|---|
| customer_name | A.P.TRANSFORMERS | 0.95 | correct |
| customer_address | SP24 & 95H/1(SP), Ambattur Indl Estate CHENNAI - 600058 | 0.95 | correct |
| samples | 3 Phase Distribution Transformer | 0.95 | correct |
| rating | 250 KVA / 11 KV / 433V, 3PH, 50 HZ Copper wand | 0.9 | correct |
| pf_kva | 250 | 0.9 | correct |
| pf_hv_v | 11000 | 0.9 | correct |
| pf_lv_v | 433 | 0.9 | correct |
| serial_numbers | 1098 | 0.98 | correct |
| num_samples | 1 | 0.95 | correct |
| witness_customer | Mr Vivekanand Krishna
Customer representative | 0.9 | correct |
| criteria | AS per Standard | 0.95 | correct |
| take_back | Yes | 0.95 | correct |
| soc_required | Yes | 0.95 | correct |
| decision_rule | (i) | 0.95 | correct |
| msme | Yes | 0.95 | correct |
| @drawings | APT/250-11/001, APT/250-11/001B |  | correct |

## Proforma for transformers (scanned)

| Field | Read | Confidence | Result |
|---|---|---|---|
| pf_vector_group | Dyn11 | 0.98 | correct |
| pf_cooling | ONAN | 0.95 | correct |
| pf_freq | 50 | 0.98 | correct |
| pf_impedance | 4.50 | 0.9 | correct |
| pf_um | 12 | 0.95 | correct |
| pf_max_tl50 | 980 | 0.95 | correct |
| pf_max_tl100 | 2930 | 0.95 | correct |
| pf_oil_volume | 460 | 0.95 | correct |
| pf_mfg | 10/2025 | 0.95 | correct |
| pf_sealing | Non-sealed | 0.98 | correct |
| pf_insulation | Oil immersed | 0.98 | correct |
| pf_location | Outdoor | 0.98 | correct |
| pf_winding_material | Copper | 0.98 | correct |
| pf_coil_shape | Circular | 0.98 | correct |
| pf_core | CRGO | 0.98 | correct |
| @tests | nlc_112, oil_leak, pressure_type, sc_withstand, temp_rise |  | correct |

## Routine test logsheet (scanned)

| Field | Read | Confidence | Result |
|---|---|---|---|
| ir.bt.hv_e | 4.19 | 0.9 | correct |
| ir.at.hv_e | 6.11 | 0.9 | correct |
| ir.bt.lv_e | 1.22 | 0.95 | correct |
| ir.at.lv_e | 1.35 | 0.95 | correct |
| ir.bt.hv_lv | 6.56 | 0.9 | wrong |
| ir.at.hv_lv | 7.81 | 0.9 | correct |
| iov.bt.v | 866 | 0.95 | correct |
| iov.at.v | 866 | 0.95 | correct |
| iov.bt.a | 1.58 | 0.95 | correct |
| iov.at.a | 1.66 | 0.95 | correct |
| iov.bt.hz | 100 | 0.95 | correct |
| iov.bt.s | 60 | 0.95 | correct |
| ratio.t1.bt.u | 46.38 | 0.95 | correct |
| ratio.t1.bt.v | 46.36 | 0.95 | correct |
| ratio.t1.bt.w | 46.26 | 0.95 | correct |
| ratio.t1.at.u | 46.37 | 0.95 | correct |
| ratio.t1.at.v | 46.37 | 0.95 | correct |
| ratio.t1.at.w | 46.24 | 0.95 | wrong |
| ratio.t2.bt.u | 45.22 | 0.95 | correct |
| ratio.t2.bt.v | 45.22 | 0.95 | correct |
| ratio.t2.bt.w | 45.12 | 0.95 | correct |
| ratio.t2.at.u | 45.23 | 0.95 | correct |
| ratio.t2.at.v | 45.23 | 0.95 | correct |
| ratio.t2.at.w | 45.13 | 0.95 | correct |
| ratio.t3.bt.u | 44.10 | 0.95 | correct |
| ratio.t3.bt.v | 44.09 | 0.95 | correct |
| ratio.t3.bt.w | 44.06 | 0.95 | correct |
| ratio.t3.at.u | 44.11 | 0.95 | correct |
| ratio.t3.at.v | 44.09 | 0.95 | correct |
| ratio.t3.at.w | 44.07 | 0.95 | correct |
| ratio.t4.bt.u | 43.02 | 0.95 | correct |
| ratio.t4.bt.v | 43.01 | 0.95 | correct |
| ratio.t4.bt.w | 42.96 | 0.95 | correct |
| ratio.t4.at.u | 43.03 | 0.95 | correct |
| ratio.t4.at.v | 43.05 | 0.95 | correct |
| ratio.t4.at.w | 43.00 | 0.95 | correct |
| ratio.t5.bt.u | 41.92 | 0.95 | correct |
| ratio.t5.bt.v | 41.92 | 0.95 | correct |
| ratio.t5.bt.w | 41.92 | 0.95 | correct |
| ratio.t5.at.u | 41.93 | 0.95 | correct |
| ratio.t5.at.v | 41.92 | 0.95 | correct |
| ratio.t5.at.w | 41.92 | 0.95 | correct |
| ratio.t6.bt.u | 40.82 | 0.95 | correct |
| ratio.t6.bt.v | 40.86 | 0.95 | correct |
| ratio.t6.bt.w | 40.86 | 0.95 | correct |
| ratio.t6.at.u | 40.81 | 0.95 | correct |
| ratio.t6.at.v | 40.86 | 0.95 | correct |
| ratio.t6.at.w | 40.81 | 0.95 | correct |
| ratio.t7.bt.u | 39.73 | 0.95 | correct |
| ratio.t7.bt.v | 39.72 | 0.95 | correct |
| ratio.t7.bt.w | 39.72 | 0.95 | correct |
| ratio.t7.at.u | 39.73 | 0.95 | correct |
| ratio.t7.at.v | 39.72 | 0.95 | correct |
| ratio.t7.at.w | 39.72 | 0.95 | correct |

## Short-circuit logsheet (scanned)

| Field | Read | Confidence | Result |
|---|---|---|---|
| sc.req.nt.rms | 7.61 | 0.95 | correct |
| sc.req.nt.pk | 16.73 | 0.95 | correct |
| sc.req.ht.rms | 7.50 | 0.95 | correct |
| sc.req.ht.pk | 16.61 | 0.95 | correct |
| sc.req.lt.rms | 7.43 | 0.95 | correct |
| sc.req.lt.pk | 16.20 | 0.95 | correct |
| sc.s001.pk | 5.32 | 0.95 | correct |
| sc.s001.rms_avg | 2.08 | 0.95 | correct |
| sc.s001.dur | 0.54 | 0.95 | correct |
| sc.s002.pk | 16.80 | 0.95 | correct |
| sc.s002.rms_avg | 7.53 | 0.95 | correct |
| sc.s002.dur | 0.54 | 0.95 | correct |
| sc.s003.pk | 16.82 | 0.95 | correct |
| sc.s003.rms_avg | 7.66 | 0.95 | correct |
| sc.s003.dur | 0.54 | 0.95 | correct |
| sc.s004.pk | 16.74 | 0.95 | correct |
| sc.s004.rms_avg | 7.71 | 0.95 | correct |
| sc.s004.dur | 0.54 | 0.95 | correct |
| sc.s005.pk | 16.89 | 0.95 | correct |
| sc.s005.rms_avg | 7.52 | 0.95 | correct |
| sc.s005.dur | 0.54 | 0.95 | correct |
| sc.s006.pk | 16.78 | 0.95 | correct |
| sc.s006.rms_avg | 7.54 | 0.95 | correct |
| sc.s006.dur | 0.54 | 0.95 | correct |
| sc.s007.pk | 16.72 | 0.95 | correct |
| sc.s007.rms_avg | 7.53 | 0.95 | correct |
| sc.s007.dur | 0.54 | 0.95 | correct |
| sc.s008.pk | 16.49 | 0.95 | correct |
| sc.s008.rms_avg | 7.63 | 0.95 | correct |
| sc.s008.dur | 0.54 | 0.95 | correct |
| sc.s009.pk | 16.34 | 0.95 | correct |
| sc.s009.rms_avg | 7.44 | 0.95 | correct |
| sc.s009.dur | 0.54 | 0.95 | correct |
| sc.s010.pk | 16.26 | 0.95 | correct |
| sc.s010.rms_avg | 7.45 | 0.95 | correct |
| sc.s010.dur | 0.54 | 0.95 | correct |
| sc.s011.rms_avg | 7.55 | 0.95 | correct |
| sc.s011.dur | 2.01 | 0.95 | correct |

## Losses workbook, foreign layout (Excel)

| Field | Read | Confidence | Result |
|---|---|---|---|
| nll.bt.f | 49.95 @ Loss test record!B5 | 0.95 | correct |
| nll.bt.p | 383.18 @ Loss test record!C5 | 0.95 | correct |
| nll.bt.i | 1.689 @ Loss test record!D5 | 0.95 | correct |
| nll.bt.v | 433.05 @ Loss test record!E5 | 0.95 | correct |
| nll.at.f | 49.99 @ Loss test record!B6 | 0.95 | correct |
| nll.at.p | 383.26 @ Loss test record!C6 | 0.95 | correct |
| nll.at.i | 1.687 @ Loss test record!D6 | 0.95 | correct |
| nll.at.v | 433.02 @ Loss test record!E6 | 0.95 | correct |
| ll.nt.bt.p1 | 568.91 @ Loss test record!C11 | 0.95 | correct |
| ll.nt.bt.p2 | 565.64 @ Loss test record!D11 | 0.95 | correct |
| ll.nt.bt.p3 | 542.57 @ Loss test record!E11 | 0.95 | correct |
| ll.nt.bt.i1 | 12.706 @ Loss test record!F11 | 0.95 | correct |
| ll.nt.bt.i2 | 12.718 @ Loss test record!G11 | 0.95 | correct |
| ll.nt.bt.i3 | 12.576 @ Loss test record!H11 | 0.95 | correct |
| ll.nt.bt.v1 | 267.85 @ Loss test record!I11 | 0.95 | correct |
| ll.nt.bt.v2 | 268.49 @ Loss test record!J11 | 0.95 | correct |
| ll.nt.bt.v3 | 263.62 @ Loss test record!K11 | 0.95 | correct |
| ll.nt.bt.f | 49.886 @ Loss test record!L11 | 0.95 | correct |
| ll.nt.at.p1 | 588.76 @ Loss test record!C12 | 0.95 | correct |
| ll.nt.at.p2 | 586.96 @ Loss test record!D12 | 0.95 | correct |
| ll.nt.at.p3 | 568.04 @ Loss test record!E12 | 0.95 | correct |
| ll.nt.at.i1 | 12.936 @ Loss test record!F12 | 0.95 | correct |
| ll.nt.at.i2 | 12.935 @ Loss test record!G12 | 0.95 | correct |
| ll.nt.at.i3 | 12.819 @ Loss test record!H12 | 0.95 | correct |
| ll.nt.at.v1 | 273.45 @ Loss test record!I12 | 0.95 | correct |
| ll.nt.at.v2 | 273.81 @ Loss test record!J12 | 0.95 | correct |
| ll.nt.at.v3 | 269.6 @ Loss test record!K12 | 0.95 | correct |
| ll.nt.at.f | 49.959 @ Loss test record!L12 | 0.95 | correct |
| ll.ht.bt.p1 | 508.05 @ Loss test record!C13 | 0.95 | correct |
| ll.ht.bt.p2 | 508.62 @ Loss test record!D13 | 0.95 | correct |
| ll.ht.bt.p3 | 490.73 @ Loss test record!E13 | 0.95 | correct |
| ll.ht.bt.i1 | 11.607 @ Loss test record!F13 | 0.95 | correct |
| ll.ht.bt.i2 | 11.588 @ Loss test record!G13 | 0.95 | correct |
| ll.ht.bt.i3 | 11.48 @ Loss test record!H13 | 0.95 | correct |
| ll.ht.bt.v1 | 273.45 @ Loss test record!I13 | 0.95 | correct |
| ll.ht.bt.v2 | 273.66 @ Loss test record!J13 | 0.95 | correct |
| ll.ht.bt.v3 | 269.03 @ Loss test record!K13 | 0.95 | correct |
| ll.ht.bt.f | 49.876 @ Loss test record!L13 | 0.95 | correct |
| ll.ht.at.p1 | 498.22 @ Loss test record!C14 | 0.95 | correct |
| ll.ht.at.p2 | 493.92 @ Loss test record!D14 | 0.95 | correct |
| ll.ht.at.p3 | 482.26 @ Loss test record!E14 | 0.95 | correct |
| ll.ht.at.i1 | 11.452 @ Loss test record!F14 | 0.95 | correct |
| ll.ht.at.i2 | 11.449 @ Loss test record!G14 | 0.95 | correct |
| ll.ht.at.i3 | 11.342 @ Loss test record!H14 | 0.95 | correct |
| ll.ht.at.v1 | 270.67 @ Loss test record!I14 | 0.95 | correct |
| ll.ht.at.v2 | 271.09 @ Loss test record!J14 | 0.95 | correct |
| ll.ht.at.v3 | 266.12 @ Loss test record!K14 | 0.95 | correct |
| ll.ht.at.f | 49.944 @ Loss test record!L14 | 0.95 | correct |
| ll.lt.bt.p1 | 615.33 @ Loss test record!C15 | 0.95 | correct |
| ll.lt.bt.p2 | 598.64 @ Loss test record!D15 | 0.95 | correct |
| ll.lt.bt.p3 | 579.45 @ Loss test record!E15 | 0.95 | correct |
| ll.lt.bt.i1 | 14.175 @ Loss test record!F15 | 0.95 | correct |
| ll.lt.bt.i2 | 14.158 @ Loss test record!G15 | 0.95 | correct |
| ll.lt.bt.i3 | 13.954 @ Loss test record!H15 | 0.95 | correct |
| ll.lt.bt.v1 | 247.35 @ Loss test record!I15 | 0.95 | correct |
| ll.lt.bt.v2 | 248.33 @ Loss test record!J15 | 0.95 | correct |
| ll.lt.bt.v3 | 242.53 @ Loss test record!K15 | 0.95 | correct |
| ll.lt.bt.f | 49.878 @ Loss test record!L15 | 0.95 | correct |
| ll.lt.at.p1 | 590.73 @ Loss test record!C16 | 0.95 | correct |
| ll.lt.at.p2 | 577.67 @ Loss test record!D16 | 0.95 | correct |
| ll.lt.at.p3 | 557.86 @ Loss test record!E16 | 0.95 | correct |
| ll.lt.at.i1 | 13.866 @ Loss test record!F16 | 0.95 | correct |
| ll.lt.at.i2 | 13.869 @ Loss test record!G16 | 0.95 | correct |
| ll.lt.at.i3 | 13.672 @ Loss test record!H16 | 0.95 | correct |
| ll.lt.at.v1 | 242.61 @ Loss test record!I16 | 0.95 | correct |
| ll.lt.at.v2 | 244.44 @ Loss test record!J16 | 0.95 | correct |
| ll.lt.at.v3 | 238.47 @ Loss test record!K16 | 0.95 | correct |
| ll.lt.at.f | 49.955 @ Loss test record!L16 | 0.95 | correct |
