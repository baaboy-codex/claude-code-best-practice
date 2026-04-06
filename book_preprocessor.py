#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
书籍预处理器 - 分章 + SQLite 建库
用于 Phase 0 数据准备
"""

import re
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from collections import Counter

# 章节标题正则模式（覆盖常见格式）
CHAPTER_PATTERNS = [
    r'^第[零一二三四五六七八九十百千万]+[章节回集卷部篇][^\n]*',  # 第一章、第一百章
    r'^第\d+[章节回集卷部篇][^\n]*',  # 第1章、第100章（阿拉伯数字）
    r'^[第]?\d+[\.\、\s][^\n]*',  # 1. xxx, 第1 xxx
    r'^[零一二三四五六七八九十百千万]+[\.\、\s][^\n]*',  # 一、xxx
    r'^卷[零一二三四五六七八九十百千万]+[^\n]*',  # 卷一
    r'^卷\d+[^\n]*',  # 卷1（阿拉伯数字）
    r'^[序楔引子末尾终][章回集部篇]?[^\n]*',  # 序章、楔子、引子
    r'^番外[^\n]*',  # 番外
    r'^尾声[^\n]*',  # 尾声
    r'^后记[^\n]*',  # 后记
]

# 需要清洗的标题元信息
TITLE_NOISE_PATTERNS = [
    r'求[票推荐收藏点击打赏]*[！!]*',
    r'第[一二三四五六七八九十]+更[！!]*',
    r'爆更[！!]*',
    r'加更[！!]*',
    r'\d{4}年\d{1,2}月\d{1,2}日',
]

class BookPreprocessor:
    def __init__(self, book_path, output_dir):
        self.book_path = Path(book_path)
        self.output_dir = Path(output_dir)
        self.book_name = self.book_path.stem
        self.chapters = []
        self.stats = {}

    def run(self):
        """主入口"""
        print(f"\n{'='*60}")
        print(f"开始处理: {self.book_name}")
        print(f"{'='*60}")

        # 1. 读取文件
        content = self._read_file()
        if not content:
            print(f"错误: 无法读取文件 {self.book_path}")
            return False

        # 2. 分章
        self._split_chapters(content)
        print(f"分章完成: 共 {len(self.chapters)} 章")

        # 3. 保存分章文件
        self._save_chapters()

        # 4. 创建 SQLite 数据库
        self._create_database()

        # 5. 生成基础统计
        self._generate_basic_stats()

        print(f"\n处理完成: {self.book_name}")
        print(f"输出目录: {self.output_dir / self.book_name}")
        return True

    def _read_file(self):
        """读取文件，支持多种编码"""
        encodings = ['utf-8', 'gbk', 'gb2312', 'gb18030', 'big5']

        for encoding in encodings:
            try:
                with open(self.book_path, 'r', encoding=encoding) as f:
                    content = f.read()
                    print(f"文件编码: {encoding}")
                    return content
            except (UnicodeDecodeError, FileNotFoundError):
                continue

        return None

    def _split_chapters(self, content):
        """分章处理"""
        lines = content.splitlines()

        current_chapter = None
        current_content = []
        chapter_num = 0

        for line in lines:
            line_stripped = line.strip()

            # 检测章节标题
            is_chapter_title = False
            for pattern in CHAPTER_PATTERNS:
                if re.match(pattern, line_stripped):
                    is_chapter_title = True
                    break

            if is_chapter_title and len(line_stripped) < 100:  # 标题一般不超过100字
                # 保存上一章
                if current_chapter and current_content:
                    self.chapters.append({
                        'num': chapter_num,
                        'title': current_chapter,
                        'content': '\n'.join(current_content),
                        'word_count': len(''.join(current_content))
                    })

                # 开始新章
                chapter_num += 1
                current_chapter = self._clean_title(line_stripped)
                current_content = []
            else:
                if current_chapter:  # 只在检测到第一章后才开始收集内容
                    current_content.append(line)

        # 保存最后一章
        if current_chapter and current_content:
            self.chapters.append({
                'num': chapter_num,
                'title': current_chapter,
                'content': '\n'.join(current_content),
                'word_count': len(''.join(current_content))
            })

    def _clean_title(self, title):
        """清洗标题中的元信息"""
        for pattern in TITLE_NOISE_PATTERNS:
            title = re.sub(pattern, '', title)
        return title.strip()

    def _save_chapters(self):
        """保存分章文件"""
        chapter_dir = self.output_dir / self.book_name / 'chapters'
        chapter_dir.mkdir(parents=True, exist_ok=True)

        for ch in self.chapters:
            filename = f"chapter_{ch['num']:04d}.txt"
            filepath = chapter_dir / filename

            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(f"【{ch['title']}】\n\n{ch['content']}")

        print(f"分章文件已保存到: {chapter_dir}")

    def _create_database(self):
        """创建 SQLite 数据库"""
        db_path = self.output_dir / 'distillation.db'

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # 创建书籍表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                chapter_count INTEGER,
                total_words INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 创建章节表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER,
                chapter_num INTEGER,
                title TEXT,
                content TEXT,
                word_count INTEGER,
                FOREIGN KEY (book_id) REFERENCES books(id)
            )
        ''')

        # 创建知识条目表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id INTEGER,
                warehouse TEXT,
                topic TEXT,
                unit_type TEXT,
                granularity TEXT,
                title TEXT,
                tier_level TEXT,
                data_source TEXT,
                quality_axis_tags TEXT,
                target_tags TEXT,
                stage_tags TEXT,
                reader_effect_tags TEXT,
                control_tags TEXT,
                risk_tags TEXT,
                mechanism TEXT,
                applicable_conditions TEXT,
                failure_conditions TEXT,
                evidence_text TEXT,
                t1_data_evidence TEXT,
                t2_summary_evidence TEXT,
                confidence TEXT,
                recommended INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (book_id) REFERENCES books(id)
            )
        ''')

        # 插入书籍信息
        total_words = sum(ch['word_count'] for ch in self.chapters)

        cursor.execute('''
            INSERT OR REPLACE INTO books (name, chapter_count, total_words)
            VALUES (?, ?, ?)
        ''', (self.book_name, len(self.chapters), total_words))

        # 验证 lastrowid 有效性（INSERT OR REPLACE 可能在替换时返回错误 ID）
        book_id = cursor.lastrowid
        if book_id is None:
            # 查询已存在的 book_id
            cursor.execute('SELECT id FROM books WHERE name = ?', (self.book_name,))
            result = cursor.fetchone()
            if result is None:
                raise RuntimeError("书籍插入失败且无法获取有效的 book_id")
            book_id = result[0]

        # 插入章节
        for ch in self.chapters:
            cursor.execute('''
                INSERT INTO chapters (book_id, chapter_num, title, content, word_count)
                VALUES (?, ?, ?, ?, ?)
            ''', (book_id, ch['num'], ch['title'], ch['content'], ch['word_count']))

        conn.commit()
        conn.close()

        print(f"数据库已创建: {db_path}")

        return book_id

    def _generate_basic_stats(self):
        """生成基础统计"""
        if not self.chapters:
            return

        word_counts = [ch['word_count'] for ch in self.chapters]

        stats = {
            'book_name': self.book_name,
            'chapter_count': len(self.chapters),
            'total_words': sum(word_counts),
            'avg_words_per_chapter': sum(word_counts) / len(word_counts) if word_counts else 0,
            'min_words': min(word_counts) if word_counts else 0,
            'max_words': max(word_counts) if word_counts else 0,
            'processed_at': datetime.now().isoformat()
        }

        stats_path = self.output_dir / self.book_name / 'basic_stats.json'
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

        self.stats = stats
        print(f"基础统计已保存: {stats_path}")


def main():
    import sys

    if len(sys.argv) < 3:
        print("用法: python book_preprocessor.py <书籍路径> <输出目录>")
        print("示例: python book_preprocessor.py ./books/novel.txt ./output/")
        sys.exit(1)

    book_path = sys.argv[1]
    output_dir = sys.argv[2]

    preprocessor = BookPreprocessor(book_path, output_dir)
    success = preprocessor.run()

    sys.exit(1 if not success else 0)


if __name__ == '__main__':
    main()
