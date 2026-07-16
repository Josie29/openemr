--
--  AI Clinical Co-Pilot — module install schema.
--
--  Run by InstModuleTable::installSQL() via SqlUpgradeService, so this file uses
--  OpenEMR's upgrade-directive format (#IfNotTable / #EndIf) and is idempotent.
--
--  Keep semicolons out of string constants (comments included). SqlUpgradeService
--  tolerates them, but OpenEMR's other install path splits statements on ';' and
--  would mangle this file — see InstModuleTable::installSQLWithLineSplitter.
--
--  @package   OpenEMR\Modules\AiCopilot
--  @link      https://www.open-emr.org
--  @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
--

#IfNotTable ai_copilot_document_facts
CREATE TABLE `ai_copilot_document_facts` (
  `id`           bigint(20)   NOT NULL AUTO_INCREMENT,
  `document_id`  bigint(20)   NOT NULL            COMMENT 'References documents.id — the source document the fact was extracted from',
  `content_hash` char(64)     NOT NULL            COMMENT 'SHA-256 of the source bytes — with document_id this is the extraction version key (W2_ARCHITECTURE 3.4)',
  `pid`          bigint(20)   NOT NULL            COMMENT 'Patient scope — mirrors documents.foreign_id',
  `fact_table`   varchar(31)  NOT NULL            COMMENT 'Destination the fact was written to: lists | procedure_result',
  `fact_id`      bigint(20)   NOT NULL            COMMENT 'Primary key of the written row within fact_table',
  `field`        varchar(64)  NOT NULL            COMMENT 'Extracted field identity — a LOINC result code for labs, an intake field name otherwise',
  `page`         int(11)      NOT NULL DEFAULT 1  COMMENT '1-based page of the citation within the source document',
  `bbox`         varchar(255) NOT NULL DEFAULT '' COMMENT 'JSON {x,y,w,h} in PDF points (scale-1 space, as source-view.php expects) — empty when no box resolved',
  `confidence`   decimal(4,3)          DEFAULT NULL COMMENT 'Extractor-reported confidence 0.000-1.000, NULL when not reported',
  `created_at`   datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `created_by`   varchar(255) NOT NULL DEFAULT '' COMMENT 'Username whose session authorized the write',
  PRIMARY KEY (`id`),
  UNIQUE KEY `unique_document_fact` (`document_id`, `content_hash`, `fact_table`, `field`),
  KEY `document_version` (`document_id`, `content_hash`),
  KEY `pid` (`pid`)
) ENGINE=InnoDB COMMENT='AI Co-Pilot extraction sidecar: derived facts + citation geometry. A rebuildable derived cache keyed to (document_id, content_hash), NOT a system of record — OpenEMR remains the source of truth (W2_ARCHITECTURE 6).';
#EndIf
