from flask import Flask, request, jsonify
from flask_cors import CORS
import mysql.connector
from datetime import datetime
import base64
import json 

app = Flask(__name__)
CORS(app)

# ===== KONFIGURASI DATABASE =====
DB_CONFIG = {
    'host': 'mysql.railway.internal',
    'user': 'root',
    'password': 'JtelxPAHYJXuNNPLUegpiQZEtbfVCgJA',
    'database': 'railway',
    'port': 3306
}

# ✅ Track gambar yang sudah dikirim berdasarkan ID
sent_image_ids = set()

def get_db_connection():
    """Koneksi ke database"""
    try:
        conn = mysql.connector.connect(**DB_CONFIG)
        return conn
    except mysql.connector.Error as err:
        print(f"❌ Database connection error: {err}")
        return None

def ensure_summary_id_column():
    """Pastikan kolom summary_id ada di tabel detections"""
    try:
        conn = get_db_connection()
        if not conn:
            return False
        
        cursor = conn.cursor()
        
        # Cek apakah kolom summary_id sudah ada
        cursor.execute("""
            SELECT COUNT(*) as count
            FROM information_schema.COLUMNS 
            WHERE TABLE_SCHEMA = 'railway' 
            AND TABLE_NAME = 'detections' 
            AND COLUMN_NAME = 'summary_id'
        """)
        
        result = cursor.fetchone()
        
        if result[0] == 0:
            print("⚠️ Kolom summary_id belum ada, menambahkan...")
            cursor.execute("""
                ALTER TABLE detections 
                ADD COLUMN summary_id INT NULL AFTER id,
                ADD INDEX idx_summary_id (summary_id)
            """)
            conn.commit()
            print("✅ Kolom summary_id berhasil ditambahkan!")
        
        cursor.close()
        conn.close()
        return True
        
    except Exception as e:
        print(f"❌ Error checking/adding summary_id: {e}")
        return False

# ===== ENDPOINT: Simpan Deteksi dari Tkinter =====
@app.route('/api/detection', methods=['POST'])
def save_detection():
    """
    ✅ FIXED: Simpan detection_summary DULU, baru detections dengan summary_id
    """
    try:
        data = request.json
        
        # 🔥 VALIDASI INPUT
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400
            
        image_base64 = data.get('image_base64', '')
        detections = data.get('detections', [])
        
        print(f"📦 Received data:")
        print(f"   - Image size: {len(image_base64)} chars")
        print(f"   - Detections: {len(detections)}")
        
        if not image_base64:
            return jsonify({'success': False, 'error': 'No image data'}), 400
            
        if not detections:
            return jsonify({'success': False, 'error': 'No detections'}), 400
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor()
        
        # 🔥 STEP 1: Simpan ke detection_summary TERLEBIH DAHULU
        max_confidence = max([float(det.get('confidence', 0)) for det in detections])
        
        cursor.execute("""
            INSERT INTO detection_summary 
            (image_base64, total_pests_found, pest_details, max_confidence)
            VALUES (%s, %s, %s, %s)
        """, (
            image_base64, 
            len(detections), 
            json.dumps(detections),
            float(max_confidence)
        ))
        
        # ✅ Dapatkan summary_id yang baru saja di-insert
        summary_id = cursor.lastrowid
        print(f"✅ Created detection_summary with ID: {summary_id}, confidence: {max_confidence}")
        
        # 🔥 STEP 2: Simpan detail deteksi dengan summary_id
        for idx, det in enumerate(detections):
            try:
                cursor.execute("""
                    INSERT INTO detections 
                    (summary_id, pest_type, pest_name_id, confidence, 
                     location_x, location_y, width, height, total_pests)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    summary_id,  # ✅ FOREIGN KEY ke detection_summary
                    det.get('pest_type', ''),
                    det.get('pest_name_id', ''),
                    float(det.get('confidence', 0)),
                    int(det.get('x', 0)),
                    int(det.get('y', 0)),
                    int(det.get('width', 0)),
                    int(det.get('height', 0)),
                    len(detections)
                ))
                print(f"   ✅ Added detection {idx+1}/{len(detections)}: {det.get('pest_name_id', 'Unknown')}")
            except mysql.connector.Error as det_err:
                print(f"   ⚠️ Error inserting detection {idx+1}: {det_err}")
                continue
        
        # Update total deteksi
        cursor.execute("""
            UPDATE system_status 
            SET total_detections = total_detections + 1,
                last_update = NOW()
            WHERE id = 1
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"✅ Data saved successfully with summary_id: {summary_id}")
        
        return jsonify({
            'success': True,
            'id': summary_id,
            'message': f'1 gambar dengan {len(detections)} hama berhasil disimpan',
            'timestamp': datetime.now().isoformat()
        }), 201
        
    except mysql.connector.Error as db_err:
        print(f"❌ Database error: {db_err}")
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': f'Database error: {str(db_err)}'}), 500
    except Exception as e:
        print(f"❌ Error: {e}")
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== ✅ ENDPOINT: Data untuk Flutter (DENGAN SEMUA PEST NAMES) =====
@app.route('/data', methods=['GET'])
def get_data():
    """
    ✅ FIXED: Mengembalikan SEMUA nama hama dalam satu deteksi dengan confidence yang benar
    """
    global sent_image_ids
    
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor(dictionary=True)
        
        # Ambil status sistem
        cursor.execute("SELECT * FROM system_status WHERE id = 1")
        status = cursor.fetchone()
        
        if not status:
            cursor.execute("INSERT INTO system_status (id, system_active, total_detections) VALUES (1, TRUE, 0)")
            conn.commit()
            status = {'system_active': True, 'total_detections': 0}
        
        # ✅ AMBIL DETECTION_SUMMARY TERLEBIH DAHULU (DENGAN pest_details)
        # 🔥 FIX: Cast max_confidence sebagai DECIMAL untuk memastikan nilainya terbaca
        if sent_image_ids:
            placeholders = ','.join(['%s'] * len(sent_image_ids))
            query = f"""
                SELECT 
                    id,
                    detection_time,
                    image_base64,
                    CAST(max_confidence AS DECIMAL(10,2)) as max_confidence,
                    total_pests_found,
                    pest_details
                FROM detection_summary
                WHERE id NOT IN ({placeholders})
                ORDER BY detection_time DESC 
                LIMIT 1
            """
            cursor.execute(query, tuple(sent_image_ids))
        else:
            cursor.execute("""
                SELECT 
                    id,
                    detection_time,
                    image_base64,
                    CAST(max_confidence AS DECIMAL(10,2)) as max_confidence,
                    total_pests_found,
                    pest_details
                FROM detection_summary
                ORDER BY detection_time DESC 
                LIMIT 1
            """)
        
        latest = cursor.fetchone()
        
        # ✅ JIKA ADA DETEKSI BARU, AMBIL SEMUA NAMA HAMA-NYA
        pest_names = []
        if latest:
            # Try dengan summary_id dulu
            cursor.execute("""
                SELECT pest_name_id, MAX(confidence) as max_conf
                FROM detections 
                WHERE summary_id = %s
                GROUP BY pest_name_id
                ORDER BY max_conf DESC
            """, (latest['id'],))
            
            pest_results = cursor.fetchall()
            pest_names = [p['pest_name_id'] for p in pest_results if p['pest_name_id']]
            
            # ✅ Fallback: Jika tidak ada hasil, coba dari pest_details JSON
            if not pest_names:
                try:
                    pest_details = json.loads(latest.get('pest_details', '[]')) if 'pest_details' in latest else []
                    pest_names = list(set([d.get('pest_name_id') for d in pest_details if d.get('pest_name_id')]))
                    print(f"   📝 Using pest_details JSON: {pest_names}")
                except:
                    pest_names = ['Unknown Pest']
        
        # Ambil waktu deteksi terakhir
        cursor.execute("""
            SELECT detection_time FROM detection_summary 
            ORDER BY detection_time DESC 
            LIMIT 1
        """)
        last_detection_record = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        # Base response
        response = {
            'motion': False,
            'totalDetections': status['total_detections'],
            'lastDetection': last_detection_record['detection_time'].strftime('%Y-%m-%d %H:%M:%S') if last_detection_record else '-',
            'systemActive': bool(status['system_active']),
            'newDetection': False,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'confidence': 85,
            'pestName': 'Unknown Pest',
            'pestNames': []
        }
        
        # ✅ Kirim gambar HANYA jika ada deteksi baru
        if latest and latest['id'] not in sent_image_ids:
            # 🔥 FIX: Ambil confidence dengan fallback yang lebih baik
            confidence_value = latest.get('max_confidence')
            if confidence_value is not None:
                try:
                    confidence = int(float(confidence_value) * 100)  # Convert 0.95 -> 95
                except (ValueError, TypeError):
                    confidence = 85
            else:
                confidence = 85
            
            response['newDetection'] = True
            response['motion'] = True
            response['image'] = latest['image_base64']
            response['id'] = latest['id']
            response['confidence'] = confidence
            response['pestNames'] = pest_names
            response['pestName'] = ', '.join(pest_names) if pest_names else 'Unknown Pest'
            
            sent_image_ids.add(latest['id'])
            print(f"📷 Sending NEW image: ID={latest['id']}, Confidence={confidence}%, Pests={pest_names} (Total sent: {len(sent_image_ids)})")
            
            # Batasi tracking maksimal 100 ID
            if len(sent_image_ids) > 100:
                sorted_ids = sorted(sent_image_ids)
                ids_to_remove = sorted_ids[:50]
                sent_image_ids -= set(ids_to_remove)
                print(f"🗑️ Cleaned up old IDs, remaining: {len(sent_image_ids)}")
        else:
            if latest:
                print(f"⏭️ Image ID={latest['id']} already sent")
        
        return jsonify(response), 200
        
    except Exception as e:
        print(f"❌ Error /data: {e}")
        return jsonify({'error': str(e)}), 500

# ===== ✅ ENDPOINT: History dengan Semua Pest Names =====
@app.route('/api/history', methods=['GET'])
def get_history():
    """
    ✅ FIXED: Endpoint untuk mengambil riwayat deteksi dengan SEMUA NAMA HAMA
    """
    try:
        limit = request.args.get('limit', 50, type=int)
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor(dictionary=True)
        
        # Ambil detection summaries (dengan pest_details)
        cursor.execute("""
            SELECT 
                id,
                detection_time as timestamp,
                image_base64 as image,
                CAST(max_confidence AS DECIMAL(10,2)) as confidence,
                total_pests_found,
                pest_details
            FROM detection_summary
            ORDER BY detection_time DESC
            LIMIT %s
        """, (limit,))
        
        history = cursor.fetchall()
        
        # ✅ Untuk setiap history, ambil semua pest names-nya
        for item in history:
            cursor.execute("""
                SELECT pest_name_id, MAX(confidence) as max_conf
                FROM detections 
                WHERE summary_id = %s
                GROUP BY pest_name_id
                ORDER BY max_conf DESC
            """, (item['id'],))
            
            pest_results = cursor.fetchall()
            pest_names = [p['pest_name_id'] for p in pest_results if p['pest_name_id']]
            
            # ✅ Fallback ke pest_details jika tidak ada di detections
            if not pest_names:
                try:
                    pest_details = json.loads(item.get('pest_details', '[]')) if 'pest_details' in item else []
                    pest_names = list(set([d.get('pest_name_id') for d in pest_details if d.get('pest_name_id')]))
                except:
                    pest_names = ['Unknown Pest']
            
            # 🔥 FIX: Convert confidence dari decimal ke integer percentage
            conf_value = item.get('confidence')
            if conf_value is not None:
                try:
                    item['confidence'] = int(float(conf_value) * 100)
                except (ValueError, TypeError):
                    item['confidence'] = 85
            else:
                item['confidence'] = 85
            
            item['timestamp'] = item['timestamp'].strftime('%Y-%m-%d %H:%M:%S')
            item['motionDetected'] = True
            item['pestNames'] = pest_names
            item['pestName'] = ', '.join(pest_names) if pest_names else 'Unknown Pest'
        
        cursor.close()
        conn.close()
        
        print(f"✅ Returning {len(history)} detection records with pest names")
        return jsonify(history), 200
        
    except Exception as e:
        print(f"❌ Error /api/history: {e}")
        return jsonify({'error': str(e)}), 500

# ===== ENDPOINT: Control dari Flutter =====
@app.route('/control', methods=['POST'])
def control():
    try:
        data = request.json
        
        if 'systemActive' in data:
            conn = get_db_connection()
            if not conn:
                return jsonify({'success': False, 'error': 'Database connection failed'}), 500
                
            cursor = conn.cursor()
            
            cursor.execute("""
                UPDATE system_status 
                SET system_active = %s, last_update = NOW()
                WHERE id = 1
            """, (bool(data['systemActive']),))
            
            conn.commit()
            cursor.close()
            conn.close()
            
            return jsonify({
                'success': True,
                'systemActive': bool(data['systemActive'])
            }), 200
        
        return jsonify({'success': False, 'error': 'Invalid parameter'}), 400
        
    except Exception as e:
        print(f"❌ Error /control: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== ✅ ENDPOINT: DELETE DETECTION (BARU) =====
@app.route('/api/delete/<int:summary_id>', methods=['DELETE'])
def delete_detection(summary_id):
    """
    ✅ Hapus deteksi berdasarkan summary_id
    """
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor()
        
        # 🔥 STEP 1: Cek apakah detection exists
        cursor.execute("""
            SELECT id FROM detection_summary 
            WHERE id = %s
        """, (summary_id,))
        
        if not cursor.fetchone():
            cursor.close()
            conn.close()
            return jsonify({
                'success': False, 
                'error': 'Detection not found'
            }), 404
        
        # 🔥 STEP 2: Hapus detail detections terlebih dahulu (child records)
        cursor.execute("""
            DELETE FROM detections 
            WHERE summary_id = %s
        """, (summary_id,))
        
        deleted_details = cursor.rowcount
        
        # 🔥 STEP 3: Hapus detection_summary (parent record)
        cursor.execute("""
            DELETE FROM detection_summary 
            WHERE id = %s
        """, (summary_id,))
        
        deleted_summary = cursor.rowcount
        
        # 🔥 STEP 4: Update total detections di system_status
        cursor.execute("""
            UPDATE system_status 
            SET total_detections = GREATEST(total_detections - 1, 0),
                last_update = NOW()
            WHERE id = 1
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        
        # 🔥 STEP 5: Hapus dari tracking sent_image_ids
        global sent_image_ids
        if summary_id in sent_image_ids:
            sent_image_ids.remove(summary_id)
        
        print(f"🗑️ Deleted detection: summary_id={summary_id}, details={deleted_details}")
        
        return jsonify({
            'success': True,
            'message': 'Detection deleted successfully',
            'deleted_details': deleted_details,
            'summary_id': summary_id
        }), 200
        
    except mysql.connector.Error as db_err:
        print(f"❌ Database error: {db_err}")
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': f'Database error: {str(db_err)}'}), 500
    except Exception as e:
        print(f"❌ Error deleting detection: {e}")
        if conn:
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== ENDPOINT: Reset Sent Images =====
@app.route('/api/reset-sent', methods=['POST'])
def reset_sent_images():
    global sent_image_ids
    old_count = len(sent_image_ids)
    sent_image_ids.clear()
    print(f"🔄 Reset sent image IDs (cleared {old_count} IDs)")
    return jsonify({
        'success': True, 
        'message': f'Sent images tracking reset ({old_count} IDs cleared)'
    }), 200

# ===== ENDPOINT: Test Connection =====
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({
        'status': 'online',
        'timestamp': datetime.now().isoformat(),
        'sent_ids_count': len(sent_image_ids)
    }), 200

# ===== ENDPOINT: Hapus History Lama =====
@app.route('/api/clear-old', methods=['DELETE'])
def clear_old_detections():
    try:
        keep_count = request.args.get('keep', 50, type=int)
        
        conn = get_db_connection()
        if not conn:
            return jsonify({'success': False, 'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor()
        
        cursor.execute(f"""
            DELETE FROM detection_summary
            WHERE id NOT IN (
                SELECT id FROM (
                    SELECT id FROM detection_summary 
                    ORDER BY detection_time DESC 
                    LIMIT {keep_count}
                ) AS keep_ids
            )
        """)
        
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"🗑️ Deleted {deleted} old detection records")
        
        return jsonify({
            'success': True,
            'deleted': deleted,
            'kept': keep_count
        }), 200
        
    except Exception as e:
        print(f"❌ Error /api/clear-old: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ===== ENDPOINT: Get Stats =====
@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        conn = get_db_connection()
        if not conn:
            return jsonify({'error': 'Database connection failed'}), 500
            
        cursor = conn.cursor(dictionary=True)
        
        cursor.execute("SELECT COUNT(*) as total FROM detection_summary")
        total = cursor.fetchone()['total']
        
        cursor.execute("""
            SELECT COUNT(*) as today 
            FROM detection_summary 
            WHERE DATE(detection_time) = CURDATE()
        """)
        today = cursor.fetchone()['today']
        
        sent_count = len(sent_image_ids)
        
        cursor.execute("""
            SELECT pest_name_id, COUNT(*) as count
            FROM detections
            GROUP BY pest_name_id
            ORDER BY count DESC
            LIMIT 1
        """)
        top_pest = cursor.fetchone()
        
        cursor.close()
        conn.close()
        
        return jsonify({
            'total_detections': total,
            'today_detections': today,
            'sent_images_tracked': sent_count,
            'most_detected_pest': top_pest['pest_name_id'] if top_pest else 'None',
            'most_detected_count': top_pest['count'] if top_pest else 0
        }), 200
        
    except Exception as e:
        print(f"❌ Error /api/stats: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    # ✅ Pastikan kolom summary_id ada saat startup
    print("🔍 Checking database structure...")
    ensure_summary_id_column()
    
    print("\n🚀 API Server berjalan di http://0.0.0.0:5000")
    print("📡 Endpoints:")
    print("   POST   /api/detection    - Simpan deteksi baru")
    print("   GET    /data             - Data terbaru (DENGAN SEMUA PEST NAMES)")
    print("   GET    /api/history      - Riwayat deteksi (DENGAN SEMUA PEST NAMES)")
    print("   POST   /control          - Kontrol sistem")
    print("   DELETE /api/delete/<id>  - Hapus deteksi by ID ✅ BARU!")
    print("   POST   /api/reset-sent   - Reset tracking gambar")
    print("   DELETE /api/clear-old    - Hapus deteksi lama")
    print("   GET    /api/stats        - Statistik deteksi")
    print("   GET    /ping             - Test koneksi")
    print("")
    print("✅ Fitur:")
    print("   • Auto-create kolom summary_id di tabel detections")
    print("   • Menampilkan SEMUA nama hama dalam satu deteksi")
    print("   • pestNames = array ['Belatung Pucuk', 'Wereng Coklat']")
    print("   • pestName = string 'Belatung Pucuk, Wereng Coklat'")
    print("   • Query menggunakan summary_id sebagai foreign key")
    print("   • Delete detection dengan cascade (summary + details)")
    print("   • 🔥 FIX: Confidence sekarang menampilkan persentase dengan benar")
    app.run(host='0.0.0.0', port=5000, debug=True)