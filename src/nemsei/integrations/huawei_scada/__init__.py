"""Huawei SDongle SCADA/NMS adapter: inbound, read-only, no edge collector.

Unlike FusionSolar and Sigenergy, nothing here calls out to a provider. The
logger dials this server, and the server answers by reading Modbus registers
back over the connection the logger opened. That inversion is why this package
owns a process (`listener.py`) rather than a client.

`listener` is deliberately not re-exported: importing it pulls in `socket`, and
the only thing that should ever do that is the listener process itself.
"""

from nemsei.integrations.huawei_scada.ingestion import HuaweiScadaIngestion
from nemsei.integrations.huawei_scada.retention import purge_samples
from nemsei.integrations.huawei_scada.rollup import HuaweiScadaRollupService
from nemsei.integrations.huawei_scada.service import contract_for

__all__ = ["HuaweiScadaIngestion", "HuaweiScadaRollupService", "contract_for", "purge_samples"]
