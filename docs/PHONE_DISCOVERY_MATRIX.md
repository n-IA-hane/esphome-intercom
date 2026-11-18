# Managed phone discovery and delivery by model

VoIP Stack can suggest a physical phone when Home Assistant sees a matching
DHCP record or the PBX receives a compatible SIP `ua-profile` announcement.
An administrator can also add the phone by MAC and select its profile.
The tables cover the 98 profiles with downloadable bundles in the referenced
catalog. Its other 104 entries contain imported source only. Each phone keeps
the catalog revision selected by its administrator. [Catalog manifest at revision 04fee24](https://github.com/n-IA-hane/voip-stack-phone-profiles/blob/04fee24de3d92fc75c4474a51cda07cc2a5e893a/manifest.json)

## What can appear from a DHCP record

Home Assistant supplies a MAC, hostname and IP address. VoIP Stack checks the
manufacturer's registered MAC prefix and a known model hostname before showing
a proposal. DHCP option 12 identifies the client hostname. A phone with a
renamed hostname can still appear through SIP PnP or be added manually.
The matching rules apply equally to new records and to HA's cached records.
[IEEE MAC registry](https://standards-oui.ieee.org/oui/oui.csv) · [Home Assistant discovery matchers](https://contributors.home-assistant.io/core/integrations/manifest/)

| Vendor | Catalog model IDs | HA DHCP recognition |
| --- | --- | --- |
| Snom | 360 | Observed `snom360` or `snom360-<suffix>`, with prefix `000413`. |
| Snom | 300, 320, 3xx, 720, 7xx, 820, 8xx | No model-specific matcher. Family profile IDs can represent multiple handsets. |
| Yealink | t23g, t23p, t27g, t27p, t29g, t31g, t33g, t40g, t40p, t41p, t41s, t42g, t42s, t42u, t43u, t46g, t46s, t46u, t48g, t48s, t52s, t53, t53w, t54s, t57w | Exact documented `SIP-<model>` hostname with a Yealink prefix, case-insensitive. |
| Yealink | w70b | Exact documented `W70B` hostname of the DECT base with a Yealink prefix. |
| Yealink | cp860, t21p, t32g, t38g, t49g, t56a, t58a, vp530, w52p, w56p, w60b, w7xp, w80 | No model-specific matcher. An E2 suffix, handset name or family ID does not establish an exact profile. |
| Fanvil | h2u-v2, v62, v62-pro, v63, v64, v65, v67, x1s, x1sg, x1sp, x210, x3sg, x3sw, x3u, x3u-pro, x4sg, x4u, x5s, x5u, x6, x6u, x7, x7a | No confirmed option 12 fingerprint in the sources reviewed. The X3U Pro release notes confirm that the phone sends option 12. |
| Grandstream | grp2612, grp2612w, grp2613, grp2614, grp2615, grp2616, gswave, gxp116x, gxp1450bk, gxp17xx, gxp20xx, gxp2100, gxp2124, gxp2130, gxp2135, gxp2140, gxp2160, gxp2170, gxp21xx, gxp21xxbk, gxp2200, gxp3240, gxv300x, gxv3140, gxv3175, gxv3175v2, wp810, wp820 | No confirmed model hostname matcher. GRP2614 exposes a configurable option 12. The gswave profile is a softphone. |

Yealink lists option 12 factory hostnames in the administration guides. The
W70B uses the base name without the SIP prefix. The integration tests exercise
model recognition and Home Assistant's native discovery flow with example
leases. Physical startup and provisioning require separate observation.
[Yealink desk phone guide V86.60](https://support.yealink.com/docs/cp920/yealink-sip-t2-series-t3-series-t4-series-t5-series-cp920-ip-phones-administrator-guide-v86-60-pdf/66e9a9edb3ac435a80105f7e6ca86820) · [Yealink desk phone guide V83.30](https://snr.systems/site/data-files/Yealink/SIP-T23/Yealink_SIP-T2_Series_T4_Series_T5_Series_IP_Phones_Administrator_Guide_V83_30.pdf) · [Yealink DECT DHCP settings](https://support.yealink.com/docs/w71p-w70b-w71h/dhcp-option-for-ipv4/5790d285c57b4db69ba83ce011d7725c) · [Fanvil firmware 2.12.20](https://fanvil.com/upload/files/20240730/222142dj2v.pdf) · [Grandstream GRP administration guide](https://documentation.grandstream.com/knowledge-base/grp261x-grp262x-grp263x-series-administration-guide/)

## How a phone can obtain its configuration

With SIP PnP the handset sends a SIP subscription at startup. A compatible
PBX can reply with a SIP `NOTIFY` containing the provisioning address. With
DHCP provisioning, the network's existing DHCP server supplies a provisioning
address in its response. A Home Assistant DHCP discovery record contains an
inventory hint and grants no right to send a private URL. Scope a private URL
in DHCP to its intended phone. A URL can also be entered in a phone's own
provisioning field. The following model evidence applies to the specified
delivery method and firmware where known.

| Model | Vendor-documented delivery | Project evidence |
| --- | --- | --- |
| Snom 360 | Multicast SIP subscription and a settings-file URL in the notification. 3CX also documents option 66 on firmware 8.7.3.19. | A handset on 8.7.3.25.9 recorded sending its startup announcement. Native HA and UDP tests exercise proposals and adoption. Download and settings readback on the office phone require a separate receipt. [Snom SIP trace](https://service.snom.com/plugins/viewsource/viewpagesrc.action?pageId=234336488) · [3CX Snom 360 guide](https://www.3cx.com/blog/docs/snom-provisioning/) |
| Yealink T46U | SIP PnP or DHCP option 66, 43 or a configured custom option supplies the provisioning server. A phone can try boot and common configuration files before the MAC-specific CFG. | The bundle renders `{mac}.cfg`. HA DHCP matching is covered in software. Hardware download and settings readback remain open. [Yealink provisioning guide](https://support.yealink.com/docs/sip-t46u/yealink-sip-ip-phones-auto-provisioning-guide-v1-5-pdf/2fc6c7bd89fb4fe692f3867ea363ff4a) |
| Fanvil X3U Pro | The manual describes SIP PnP, DHCP, a static server, and TR-069. The directory can serve the device-specific CFG file. | On firmware 2.12.20 the project observed a native apply request, matching download and changed settings. Automatic application is qualified only for this model and firmware. [Fanvil manual](https://www.fanvil.com/upload/files/20220518/134146j8vz.pdf) · [Catalog qualification](https://github.com/n-IA-hane/voip-stack-phone-profiles/blob/04fee24de3d92fc75c4474a51cda07cc2a5e893a/README.md#directory-modes-and-application-capabilities) |
| Grandstream GRP2614 | Multicast SIP provisioning is configurable. The vendor PBX sends an XML file URL by SIP and a directory by DHCP option 66. The GRP tries `cfg{mac}.xml` when looking in a directory. | Catalog revision 04fee24 declares `{mac}.xml`. A directory fetch on that revision has a filename mismatch. Hardware confirmation must record the requested path and the applied revision. [GRP manual](https://documentation.grandstream.com/knowledge-base/grp261x-grp262x-grp263x-series-administration-guide/) · [Grandstream Zero Config](https://documentation.grandstream.com/knowledge-base/zero-config-guide/) |

For other models the catalog validates the output syntax in software.
Fanvil profiles generate XML named `{mac}.cfg`. Yealink profiles generate CFG
named `{mac}.cfg`. Snom and Grandstream profiles in revision 04fee24 generate
XML named `{mac}.xml`. A model's bundle can render a file independently of
the phone's ability to discover the PBX, request that filename or apply the
settings. Legacy models, family aliases, DECT bases and software clients
require their own startup and configuration evidence.

After its first verified download, a Snom 360 with a pinned profile declaring
`snom_check_sync` can receive a targeted settings refresh for a new revision.
The integration requires a current registration identified as that handset with
the exact firmware `8.7.3.25.9` and waits while its managed lines are busy.
It sends the empty SIP notification with `Event: check-sync;reboot=false`.
Asterisk documents separate commands for checking settings and rebooting. A
published Snom 360 test observed configuration retrieval with the explicit
false parameter. The model and firmware above have software coverage for the
wire request. A SIP success response records command acceptance. A later HTTP
download records delivery. If the phone challenges this optional notification,
the worker reports the SIP authentication error. A lost response is recorded
as uncertain. The ordinary provisioning pull remains available in either case.
On-device settings and uptime require physical
verification. [Asterisk commands](https://github.com/asterisk/asterisk/blob/22/configs/samples/pjsip_notify.conf.sample) · [Snom 360 test and parameter handling](https://lists.digium.com/pipermail/asterisk-dev/2007-March/026665.html)

## SSDP, Zeroconf and remote phones

UPnP discovery uses SSDP on `239.255.255.250:1900`. SIP PnP uses
`224.0.1.75:5060` in the supported PBX listener. Zeroconf discovery uses
DNS-SD service records. The reviewed sources supply no model-identifying
SSDP descriptor or DNS-SD service and properties sufficient for a native HA
matcher for these 98 profiles. Both routes remain unqualified. A recorded
descriptor or service advertisement that identifies the model would allow
testing that specific route. [UPnP Device Architecture](https://openconnectivity.org/upnp-specs/UPnP-arch-DeviceArchitecture-v2.0-20200417.pdf) · [DNS-Based Service Discovery](https://www.rfc-editor.org/rfc/rfc6763.html)

For a phone at another site, the administrator can configure its private
provisioning URL directly or via that site's DHCP server where the handset
supports it. The phone needs a route to the configured origin and support
for its transport and certificate. Manufacturer services can also redirect
enrolled devices. VoIP Stack has no vendor-cloud enrollment or TR-069 client
management integration. Cloud availability depends on model, firmware and the
manufacturer's account requirements. [Snom SRAPS](https://service.snom.com/display/wiki/SRAPS) · [Yealink RPS](https://support.yealink.com/docs/yealink-management-cloud-service-ymcs/manage-rps-devices/96d8bcd22ec4405aa64d9d511819b301) · [Fanvil FDMS and FDPS](https://download.fanvil.com/FDMS/FDMS_UserManual_EN.pdf) · [Grandstream GDMS](https://www.grandstream.com/products/device-management/gdms/product/gdms)
