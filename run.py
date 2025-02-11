import os
import sys
from flask import Flask, Response, render_template, jsonify, send_from_directory, request
from werkzeug.utils import secure_filename
import cv2
import mediapipe as mp
import numpy as np
import logging
import time
from flask_socketio import SocketIO, emit
from camera.manager import CameraManager
from pose.drawer import PoseDrawer  # 确保从正确的路径导入
from connect.pose_sender import PoseSender
from connect.socket_manager import SocketManager
from config import settings
from config.settings import CAMERA_CONFIG, POSE_CONFIG
from audio.processor import AudioProcessor
from pose.pose_binding import PoseBinding
from pose.detector import PoseDetector
from pose.types import PoseData

# 配置日志格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 获取项目根目录的绝对路径
project_root = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(project_root, 'frontend', 'pages')
static_dir = os.path.join(project_root, 'frontend', 'static')

app = Flask(__name__, 
           template_folder=template_dir,
           static_folder=static_dir,
           static_url_path='/static')

# 初始化音频处理器
audio_processor = AudioProcessor()

# 定义上传文件夹路径
UPLOAD_FOLDER = os.path.join(project_root, 'uploads')

# 初始化 Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*")
socket_manager = SocketManager(socketio, audio_processor)
pose_sender = PoseSender(config=POSE_CONFIG)

# MediaPipe 初始化
mp_pose = mp.solutions.pose
mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils
mp_drawing_styles = mp.solutions.drawing_styles
mp_face_mesh = mp.solutions.face_mesh

# 初始化 MediaPipe 模型
pose = mp_pose.Pose(
    static_image_mode=False,
    model_complexity=2,
    enable_segmentation=True,
    smooth_landmarks=True,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

hands = mp_hands.Hands(
    static_image_mode=False,
    max_num_hands=2,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

face_mesh = mp_face_mesh.FaceMesh(
    static_image_mode=False,
    max_num_faces=1,
    min_detection_confidence=0.5,
    min_tracking_confidence=0.5
)

# 全局变量
camera_manager = CameraManager(config=CAMERA_CONFIG)
pose_drawer = PoseDrawer()
pose_binding = PoseBinding()
initial_frame = None
initial_regions = None

# 初始化处理器
audio_processor = AudioProcessor()
audio_processor.set_socketio(socketio)

def check_camera_settings(cap):
    """检查摄像头实际参数"""
    logger.info("摄像头当前参数:")
    params = {
        cv2.CAP_PROP_EXPOSURE: "曝光值",
        cv2.CAP_PROP_BRIGHTNESS: "亮度",
        cv2.CAP_PROP_CONTRAST: "对比度",
        cv2.CAP_PROP_GAIN: "增益"
    }
    
    for param, name in params.items():
        value = cap.get(param)
        logger.info(f"{name}: {value}")

@app.route('/')
def index():
    """渲染显示页面"""
    return render_template('display.html')

@app.route('/start_capture', methods=['POST'])
def start_capture():
    """启动摄像头"""
    try:
        success = camera_manager.start()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"启动摄像头失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/stop_capture', methods=['POST'])
def stop_capture():
    """停止摄像头"""
    try:
        success = camera_manager.stop()
        return jsonify({'success': success})
    except Exception as e:
        logger.error(f"停止摄像头失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/video_feed')
def video_feed():
    """视频流路由"""
    return Response(
        generate_frames(),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )

@app.route('/start_audio', methods=['POST'])
def start_audio():
    success = audio_processor.start_recording()
    return jsonify({'success': success})

@app.route('/stop_audio', methods=['POST'])
def stop_audio():
    success = audio_processor.stop_recording()
    return jsonify({'success': success})

@app.route('/check_stream_status')
def check_stream_status():
    try:
        status = {
            'video': {
                'is_streaming': camera_manager.is_running,
                'fps': camera_manager.current_fps
            },
            'audio': {
                'is_recording': audio_processor.is_recording,
                'sample_rate': audio_processor.sample_rate,
                'buffer_size': len(audio_processor.frames) if hasattr(audio_processor, 'frames') else 0
            }
        }
        return jsonify(status), 200
    except Exception as e:
        logger.error(f"获取流状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/capture_initial', methods=['POST'])
def capture_initial():
    """捕获初始参考帧"""
    global initial_frame, initial_regions
    
    try:
        success, frame = camera_manager.read()
        if not success:
            return jsonify({'success': False, 'error': 'Failed to capture frame'}), 500
            
        # 处理姿态
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pose_results = pose.process(frame_rgb)
        
        if not pose_results.pose_landmarks:
            return jsonify({'success': False, 'error': 'No pose detected'}), 400
            
        # 转换关键点格式
        keypoints = PoseDetector.mediapipe_to_keypoints(pose_results.pose_landmarks)
        pose_data = PoseData(keypoints=keypoints, timestamp=time.time(), confidence=1.0)
        
        # 创建区域绑定
        initial_frame = frame.copy()
        initial_regions = pose_binding.create_binding(frame, pose_data)
        
        return jsonify({
            'success': True,
            'timestamp': time.time()
        })
        
    except Exception as e:
        logger.error(f"捕获初始帧失败: {str(e)}")
        return jsonify({'success': False, 'error': str(e)}), 500

def generate_frames():
    """生成视频流"""
    while True:
        try:
            # 读取帧
            frame = camera_manager.read_frame()
            if frame is None:
                continue
                
            # 处理帧
            result = pose_sender.process_and_send(frame)
            if result is None:
                result = frame
                
            # 编码帧
            ret, buffer = cv2.imencode('.jpg', result)
            if not ret:
                continue
                
            # 生成字节流
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                   
        except Exception as e:
            logger.error(f"生成帧失败: {e}")
            time.sleep(0.1)

@app.route('/camera_status')
def camera_status():
    """获取摄像头状态"""
    try:
        status = {
            "isRunning": camera_manager.is_running,
            "fps": camera_manager.current_fps,
            "status": "running" if camera_manager.is_running else "stopped"
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"获取摄像头状态失败: {str(e)}")
        return jsonify({"error": str(e)}), 500

@socketio.on('connect')
def handle_connect():
    """处理客户端连接"""
    logger.info("客户端已连接")
    pose_sender.connect(socketio)

@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开连接"""
    logger.info("客户端已断开")
    pose_sender.disconnect()

@app.route('/api/upload_audio', methods=['POST'])
def upload_audio():
    """上传音频文件"""
    try:
        if 'audio' not in request.files:
            return jsonify({
                'status': 'error',
                'message': '没有上传文件'
            }), 400
            
        file = request.files['audio']
        if file.filename == '':
            return jsonify({
                'status': 'error', 
                'message': '未选择文件'
            }), 400
            
        # 确保上传目录存在
        audio_dir = os.path.join(UPLOAD_FOLDER, 'audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 保存文件
        filename = secure_filename(file.filename)
        file_path = os.path.join(audio_dir, filename)
        file.save(file_path)
        
        return jsonify({
            'status': 'success',
            'message': '音频上传成功',
            'audio_url': os.path.join('/uploads/audio', filename)
        })
        
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/audio/<filename>')
def stream_audio(filename):
    """流式传输音频文件"""
    def generate():
        audio_path = os.path.join(UPLOAD_FOLDER, 'audio', filename)
        with open(audio_path, 'rb') as audio_file:
            data = audio_file.read(1024)
            while data:
                yield data
                data = audio_file.read(1024)
                
    return Response(generate(), mimetype='audio/mpeg')

@app.errorhandler(Exception)
def handle_error(error):
    """全局错误处理"""
    logger.error(f"发生错误: {str(error)}")
    return jsonify({
        'success': False,
        'error': str(error)
    }), 500

@app.route('/camera/settings', methods=['GET', 'POST'])
def camera_settings():
    """获取或更新相机设置"""
    if request.method == 'GET':
        return jsonify(camera_manager.get_settings())
        
    settings = request.json
    success = camera_manager.update_settings(settings)
    return jsonify({'success': success})

@app.route('/camera/reset', methods=['POST'])
def reset_camera():
    """重置相机设置"""
    success = camera_manager.reset_settings()
    return jsonify({'success': success})

@app.route('/status')
def get_status():
    """获取当前状态"""
    try:
        status = {
            'camera': {
                'isActive': camera_manager.is_running,
                'fps': camera_manager.current_fps
            },
            'room': {
                'isConnected': socket_manager.is_connected,
                'roomId': socket_manager.current_room
            }
        }
        return jsonify(status)
    except Exception as e:
        logger.error(f"获取状态失败: {str(e)}")
        return jsonify({'error': str(e)}), 500

def main():
    """主函数"""
    try:
        # 启动服务器
        logger.info(f"服务器启动在 http://localhost:5000")
        logger.info(f"模板目录: {app.template_folder}")
        logger.info(f"静态文件目录: {app.static_folder}")
        
        socketio.run(app, host='0.0.0.0', port=5000, debug=True)
        
    except Exception as e:
        logger.error(f"服务器错误: {e}")
        
    finally:
        # 清理资源
        camera_manager.release()
        pose_sender.release()

if __name__ == '__main__':
    try:
        # 确保必要的目录存在
        os.makedirs('static', exist_ok=True)
        os.makedirs('templates', exist_ok=True)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        
        main()
    except Exception as e:
        logger.error(f"服务器启动失败: {str(e)}")
        sys.exit(1)