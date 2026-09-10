-- Additive. QA hours tracker (not billable, not tenure).
-- Hours are stored: qc_generated_count / (actual_target * 0.5)

CREATE TABLE IF NOT EXISTS `qa_work_tracker` (
  `qa_tracker_id` INT NOT NULL AUTO_INCREMENT,
  `qa_user_id` INT NOT NULL,
  `work_date` DATE NOT NULL,
  `activity_type` ENUM('qc_tasks','rework_qc','feedback','reporting') NOT NULL,
  `project_id` INT DEFAULT NULL,
  `task_id` INT DEFAULT NULL,
  `qc_record_id` INT DEFAULT NULL,
  `tracker_id` INT DEFAULT NULL,
  `source_table` VARCHAR(64) DEFAULT NULL,
  `source_id` INT DEFAULT NULL,
  `file_record_count` INT NOT NULL DEFAULT 0,
  `qc_generated_count` INT NOT NULL DEFAULT 0,
  `actual_target` DECIMAL(12,2) NOT NULL DEFAULT 0,
  `qa_target` DECIMAL(12,2) NOT NULL DEFAULT 0,
  `hours` DECIMAL(12,4) NOT NULL DEFAULT 0,
  `qc_status` VARCHAR(50) DEFAULT NULL,
  `notes` TEXT,
  `is_active` TINYINT NOT NULL DEFAULT 1,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`qa_tracker_id`),
  UNIQUE KEY `uq_qa_source` (`source_table`, `source_id`, `activity_type`),
  KEY `idx_qa_user_date` (`qa_user_id`, `work_date`, `activity_type`),
  KEY `idx_qa_project` (`project_id`, `task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
