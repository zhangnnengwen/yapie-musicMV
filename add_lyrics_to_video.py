#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python脚本：自动给视频添加同步歌词（艺术化版本）
支持开场信息显示和自定义艺术字体
"""

import re
import argparse
import os
from moviepy import VideoFileClip, ImageClip, CompositeVideoClip
import numpy as np
from PIL import Image, ImageDraw, ImageFont

def parse_lrc(lrc_path):
    """解析LRC歌词文件，返回歌词列表和歌曲信息"""
    lyrics = []
    info = {}  # 存储歌曲信息
    
    time_pattern = re.compile(r'\[(\d{2}):(\d{2})\.(\d{2,3})\]')
    info_pattern = re.compile(r'\[([a-z]+):(.+)\]', re.IGNORECASE)
    
    try:
        with open(lrc_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(lrc_path, 'r', encoding='gbk') as f:
            content = f.read()
    
    lines = content.split('\n')
    for line in lines:
        line = line.strip()
        
        info_match = info_pattern.match(line)
        if info_match:
            key = info_match.group(1).lower()
            value = info_match.group(2)
            info[key] = value
            continue
        
        matches = time_pattern.findall(line)
        if matches:
            text = time_pattern.sub('', line).strip()
            if not text:
                continue
            
            for match in matches:
                minutes = int(match[0])
                seconds = int(match[1])
                ms_str = match[2]
                milliseconds = int(ms_str) * 10 if len(ms_str) == 2 else int(ms_str)
                start_time = minutes * 60 + seconds + milliseconds / 1000
                lyrics.append((start_time, text))
    
    lyrics.sort(key=lambda x: x[0])
    return lyrics, info

def find_font(font_size, bold=False):
    """尝试加载合适的中文字体"""
    font_paths = [
        'XiangJiaoChengXingLingGanTi-2.ttf',  # 香蕉成型灵感体
        'msyhbd.ttc' if bold else 'msyh.ttc',  # 微软雅黑
        'simhei.ttf',         # 黑体
        'simkai.ttf',         # 楷体
        'simsun.ttc',         # 宋体
        'STKaiti.ttf',        # 华文楷体
        'STHeiti.ttf',        # 华文黑体
        '/Library/Fonts/Songti.ttc',
        '/Library/Fonts/Heiti.ttc',
        '/usr/share/fonts/wps-office/simhei.ttf',
    ]
    
    for font_path in font_paths:
        try:
            return ImageFont.truetype(font_path, font_size)
        except:
            continue
    
    return ImageFont.load_default()

def create_title_image(song_info, video_width, video_height, duration=5):
    """创建开场标题信息图片"""
    img = Image.new('RGBA', (video_width, video_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    title_font = find_font(64, bold=True)
    info_font = find_font(32)
    
    # 构建显示信息
    lines = []
    
    # 歌曲标题
    title = song_info.get('ti', '未知歌曲')
    lines.append(f'《{title}》')
    
    # 副标题/出处
    if 'al' in song_info:
        lines.append(f'《{song_info["al"]}》')
    
    # 词曲作者
    if 'by' in song_info:
        lines.append(f'词曲：{song_info["by"]}')
    elif 'lyrics' in song_info:
        lines.append(f'词：{song_info["lyrics"]}')
        if 'music' in song_info:
            lines.append(f'曲：{song_info["music"]}')
    
    # 编曲
    if 'arranger' in song_info:
        lines.append(f'编曲：{song_info["arranger"]}')
    
    # 演唱
    if 'ar' in song_info:
        lines.append(f'演唱：{song_info["ar"]}')
    
    # 居中显示
    def get_font_height(font, text):
        """获取字体高度（兼容新版Pillow）"""
        bbox = font.getbbox(text)
        return bbox[3] - bbox[1]
    
    total_height = sum(get_font_height(info_font, line) for line in lines) + 30 * (len(lines) - 1)
    start_y = (video_height - total_height) // 2 - 100
    
    y = start_y
    for i, line in enumerate(lines):
        if i == 0:
            font = title_font
            color = (255, 255, 255)
        else:
            font = info_font
            color = (200, 200, 200)
        
        text_width = draw.textbbox((0, 0), line, font=font)[2]
        x = (video_width - text_width) // 2
        
        # 添加阴影效果
        shadow_offset = 3
        draw.text((x + shadow_offset, y + shadow_offset), line, font=font, fill=(0, 0, 0, 150))
        draw.text((x, y), line, font=font, fill=color)
        
        y += get_font_height(font, line) + 30
    
    return np.array(img)

def create_lyric_image(text, font, video_width, font_color='white'):
    """创建艺术化歌词图片"""
    # 计算文字尺寸
    dummy_img = Image.new('RGBA', (1, 1))
    draw = ImageDraw.Draw(dummy_img)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    padding = 20
    img_height = text_height + padding * 2
    img_width = video_width
    
    img = Image.new('RGBA', (img_width, img_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    x = (img_width - text_width) // 2
    y = padding
    
    # 添加艺术化效果：阴影 + 描边
    shadow_offset = 4
    
    # 多层阴影增加立体感
    for offset in [1, 2, 3, 4]:
        draw.text((x + offset, y + offset), text, font=font, fill=(0, 0, 0, 50 + offset * 15))
    
    # 主文字
    draw.text((x, y), text, font=font, fill=font_color)
    
    return np.array(img)

def create_lyric_clips(lyrics, video_width, video_height, font_size=56, 
                       font_color='white', position='bottom', margin=80):
    """创建歌词字幕片段列表"""
    clips = []
    font = find_font(font_size)
    
    for i, (start_time, text) in enumerate(lyrics):
        if i < len(lyrics) - 1:
            end_time = lyrics[i+1][0]
        else:
            end_time = start_time + 5
        
        duration = end_time - start_time
        
        img_array = create_lyric_image(text, font, video_width, font_color)
        img_clip = ImageClip(img_array)
        
        clip_height = img_array.shape[0]
        if position == 'bottom':
            y_pos = video_height - clip_height - margin
        elif position == 'top':
            y_pos = margin
        else:
            y_pos = (video_height - clip_height) // 2
        
        clip = img_clip.with_position((0, y_pos)) \
                       .with_start(start_time) \
                       .with_duration(duration)
        
        clips.append(clip)
    
    return clips

def add_lyrics_to_video(input_video, input_lrc, output_video,
                        font_size=56, font_color='white', 
                        position='bottom', margin=80, preview=False,
                        show_title=True, title_duration=5):
    """主函数：给视频添加歌词"""
    if not os.path.exists(input_video):
        print(f"[ERROR] Video file not found: {input_video}")
        return
    
    if not os.path.exists(input_lrc):
        print(f"[ERROR] LRC file not found: {input_lrc}")
        return
    
    video = VideoFileClip(input_video)
    video_width, video_height = video.size
    video_duration = video.duration
    
    lyrics, song_info = parse_lrc(input_lrc)
    
    if lyrics:
        last_time = lyrics[-1][0]
        if last_time > video_duration:
            print(f"[WARN] Some lyrics exceed video duration")
    
    all_clips = [video]
    
    # 添加开场标题信息
    if show_title:
        title_img = create_title_image(song_info, video_width, video_height)
        title_clip = ImageClip(title_img).with_duration(title_duration)
        all_clips.append(title_clip)
    
    # 添加歌词字幕
    lyric_clips = create_lyric_clips(lyrics, video_width, video_height,
                                     font_size, font_color, position, margin)
    all_clips.extend(lyric_clips)
    
    final_video = CompositeVideoClip(all_clips)
    
    if preview:
        final_video = final_video.subclip(0, min(30, video_duration))
        output_video = 'preview_' + output_video
    
    try:
        final_video.write_videofile(
            output_video,
            codec='libx264',
            audio_codec='aac',
            fps=video.fps,
            threads=4,
            preset='medium',
            ffmpeg_params=[
                '-crf', '23',
                '-pix_fmt', 'yuv420p',  # 确保兼容性
                '-movflags', '+faststart',  # 支持网络流播放
                '-profile:v', 'baseline',   # 基础配置，兼容性最好
                '-level', '3.0'
            ]
        )
        print(f"[OK] Video generated: {output_video}")
    except Exception as e:
        print(f"[ERROR] Video generation failed: {e}")
        return
    
    video.close()
    final_video.close()

def main():
    parser = argparse.ArgumentParser(
        description='给视频添加同步歌词字幕（艺术化版本）',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    parser.add_argument('-v', '--video', required=True, help='输入视频文件路径')
    parser.add_argument('-l', '--lrc', required=True, help='输入LRC歌词文件路径')
    parser.add_argument('-o', '--output', required=True, help='输出视频文件路径')
    parser.add_argument('-fs', '--fontsize', type=int, default=56, help='字体大小')
    parser.add_argument('-fc', '--fontcolor', default='white', help='字体颜色')
    parser.add_argument('-pos', '--position', default='bottom', 
                        choices=['top', 'center', 'bottom'], help='字幕位置')
    parser.add_argument('-m', '--margin', type=int, default=80, help='边距')
    parser.add_argument('-p', '--preview', action='store_true', help='预览模式')
    parser.add_argument('-notitle', '--notitle', action='store_true', 
                        help='不显示开场标题')
    parser.add_argument('-td', '--titleduration', type=int, default=5, 
                        help='开场标题显示时长(秒)')
    
    args = parser.parse_args()
    
    add_lyrics_to_video(
        input_video=args.video,
        input_lrc=args.lrc,
        output_video=args.output,
        font_size=args.fontsize,
        font_color=args.fontcolor,
        position=args.position,
        margin=args.margin,
        preview=args.preview,
        show_title=not args.notitle,
        title_duration=args.titleduration
    )

if __name__ == '__main__':
    main()