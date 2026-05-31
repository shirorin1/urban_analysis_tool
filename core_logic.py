from qgis.core import (
    QgsVectorLayer,
    QgsFeature,
    QgsGeometry,
    QgsProject,
    QgsField,
    QgsFields,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsSpatialIndex,
    QgsRectangle,
)
from qgis.PyQt.QtCore import QMetaType


class UrbanAnalysisLogic:
    def __init__(self):
        # Соответствие классов опасности и радиусов СЗЗ (СанПиН 2.2.1/2.1.1.1200-03)
        self.szz_radii = {
            1: 1000,  # I класс
            2: 500,   # II класс
            3: 300,   # III класс
            4: 100,   # IV класс
            5: 50     # V класс
        }

        # Нормативы доступности по типам объектов (ТЗ)
        # Радиусы заданы в метрах
        self.accessibility_norms = {
            "schools": {"radius": 500, "min_count": 1, "label": "Школы"},
            "kindergartens": {"radius": 350, "min_count": 1, "label": "Детские сады"},
            "clinics": {"radius": 800, "min_count": 1, "label": "Поликлиники"},
            "pharmacies": {"radius": 500, "min_count": 1, "label": "Аптеки"},
            "playgrounds": {"radius": 300, "min_count": 1, "label": "Детские площадки"},
            "transport": {"radius": 500, "min_count": 1, "label": "Остановки транспорта"},
            "custom": {"radius": 500, "min_count": 1, "label": "Произвольный"},
        }

    def get_centroids(self, layer):
        """
        Если слой полигоны, возвращает центроиды. Если точки - как есть.
        """
        features = []
        for feat in layer.getFeatures():
            geom = feat.geometry()
            # ✅ QGIS 3.40+: используем числовые константы
            if geom.type() == 2:  # 2 = Polygon
                centroid = geom.centroid()
            else:  # 0=Point, 1=Line
                centroid = geom
            features.append({
                'id': feat.id(), 
                'geom': centroid, 
                'attrs': feat.attributes(),
                'feature': feat
            })
        return features

    def _point_from_geom(self, geom):
        """QgsPointXY из точечной геометрии или центроида."""
        try:
            return geom.asPoint()
        except Exception:
            return geom.centroid().asPoint()

    def _distance_between_points_meters(self, layer_crs, pt_a, pt_b):
        """
        Расстояние между двумя точками в метрах (EPSG:3857, планарное).
        Согласовано с отрисовкой буферов СЗЗ в UI.
        """
        tgt = QgsCoordinateReferenceSystem("EPSG:3857")
        g1 = QgsGeometry.fromPointXY(pt_a)
        g2 = QgsGeometry.fromPointXY(pt_b)
        if layer_crs.isValid() and tgt.isValid() and layer_crs != tgt:
            xform = QgsCoordinateTransform(layer_crs, tgt, QgsProject.instance())
            g1.transform(xform)
            g2.transform(xform)
        return float(g1.distance(g2))

    def _build_centroid_spatial_index(self, centroid_features):
        """
        Пространственный индекс по центроидам (не по полигонам).
        Возвращает (index, id_to_item).
        """
        index = QgsSpatialIndex()
        id_to_item = {}
        for item in centroid_features:
            feat = QgsFeature()
            feat.setId(item["id"])
            feat.setGeometry(item["geom"])
            index.addFeature(feat)
            id_to_item[item["id"]] = item
        return index, id_to_item

    def _search_rectangle_meters(self, layer_crs, center_pt, radius_m):
        """
        Ограничивающий прямоугольник в CRS слоя для index.intersects().
        Строится в метрах (3857), затем преобразуется в CRS слоя.
        """
        tgt = QgsCoordinateReferenceSystem("EPSG:3857")
        r = float(radius_m)
        if layer_crs.isValid() and tgt.isValid() and layer_crs != tgt:
            to_3857 = QgsCoordinateTransform(layer_crs, tgt, QgsProject.instance())
            to_layer = QgsCoordinateTransform(tgt, layer_crs, QgsProject.instance())
            center_geom = QgsGeometry.fromPointXY(center_pt)
            center_geom.transform(to_3857)
            pt = center_geom.asPoint()
            rect_3857 = QgsRectangle(pt.x() - r, pt.y() - r, pt.x() + r, pt.y() + r)
            return to_layer.transformBoundingBox(rect_3857)
        return QgsRectangle(
            center_pt.x() - r,
            center_pt.y() - r,
            center_pt.x() + r,
            center_pt.y() + r,
        )

    def _classify_accessibility(self, min_distance_m, norm_radius):
        """
        Классификация доступности по расстоянию.

        Норма: объект должен находиться не дальше norm_radius.
        Если ближайший объект дальше нормы, но не более чем на 300 м —
        считаем "Удовлетворительно". Если больше, чем на 300 м сверх нормы,
        то "Недостаточно". При отсутствии объектов тоже "Недостаточно".
        """
        # Если объектов нет вовсе
        if min_distance_m is None:
            return "Недостаточно", 0.0, 0

        delta = float(min_distance_m) - float(norm_radius)

        if delta <= 0:
            # В пределах нормы
            access_class = "Хорошо"
            coverage_percent = 100.0
            norm_satisfied = 1
        elif delta <= 300.0:
            # До 300 м сверх нормы
            access_class = "Удовлетворительно"
            coverage_percent = 50.0
            norm_satisfied = 0
        else:
            # Более чем на 300 м сверх нормы
            access_class = "Недостаточно"
            coverage_percent = 0.0
            norm_satisfied = 0

        return access_class, coverage_percent, norm_satisfied

    def calculate_accessibility(
        self,
        homes_layer,
        objects_layer,
        object_type="schools",
        custom_radius=None,
    ):
        """
        Расчет доступности и нормативной оценки.

        :param homes_layer: слой жилой застройки
        :param objects_layer: слой объектов инфраструктуры
        :param object_type: ключ из self.accessibility_norms
        :param custom_radius: при задании переопределяет нормативный радиус
        """
        # Нормативные параметры
        norm_cfg = self.accessibility_norms.get(object_type) or self.accessibility_norms["schools"]
        norm_radius = norm_cfg["radius"]
        min_count = norm_cfg.get("min_count", 1)

        # Фактически используемый радиус (либо пользовательский, либо нормативный), в метрах
        search_radius_m = float(custom_radius) if custom_radius is not None else float(norm_radius)

        results = []
        home_features = self.get_centroids(homes_layer)
        object_features = self.get_centroids(objects_layer)
        crs = homes_layer.crs()

        for home in home_features:
            count = 0
            min_distance = None
            home_geom = home["geom"]
            home_pt = self._point_from_geom(home_geom)

            for obj in object_features:
                obj_pt = self._point_from_geom(obj["geom"])
                d = self._distance_between_points_meters(crs, home_pt, obj_pt)

                if d <= search_radius_m:
                    count += 1

                if min_distance is None or d < min_distance:
                    min_distance = d

            access_class, coverage_percent, norm_satisfied = self._classify_accessibility(
                min_distance, norm_radius
            )

            results.append(
                {
                    "home_id": home["id"],
                    "count": count,
                    "feature": home["feature"],
                    "centroid_geom": home_geom,
                    "norm_radius": norm_radius,
                    "accessibility_class": access_class,
                    "coverage_percent": coverage_percent,
                    "norm_satisfied": norm_satisfied,
                    "object_type": object_type,
                }
            )
        return results

    def check_szz_violations(self, industrial_layer, homes_layer, class_field_name='hazard_class'):
        """
        Проверка нарушений СЗЗ.

        Возвращает два списка:
        - violations: дома внутри СЗЗ
        - szz_zones: параметры СЗЗ для промышленных объектов (для визуализации)
        """
        violations = []
        szz_zones = []

        industrial_features = self.get_centroids(industrial_layer)
        home_features = self.get_centroids(homes_layer)
        crs = homes_layer.crs()

        home_index, home_by_id = self._build_centroid_spatial_index(home_features)

        for ind in industrial_features:
            attrs = ind["attrs"]
            hazard_class = 5

            field_idx = industrial_layer.fields().indexOf(class_field_name)
            if field_idx != -1:
                val = attrs[field_idx]
                # В слоях часто приходят float (1.0), а ключи словаря — int (1)
                try:
                    if val is not None and val != "":
                        iv = int(round(float(val)))
                        if iv in self.szz_radii:
                            hazard_class = iv
                except (TypeError, ValueError):
                    pass

            radius_m = self.szz_radii.get(hazard_class, 50)

            ind_geom = ind["geom"]
            ind_pt = self._point_from_geom(ind_geom)

            # Сохраняем параметры СЗЗ для визуализации (центроид — тот же, что для расчёта)
            szz_zones.append(
                {
                    "industrial_id": ind["id"],
                    "industrial_feature": ind["feature"],
                    "center_geom": ind_geom,
                    "class": hazard_class,
                    "radius_m": radius_m,
                }
            )

            # Кандидаты по bbox, затем точная проверка расстояния в метрах
            search_rect = self._search_rectangle_meters(crs, ind_pt, radius_m)
            candidate_ids = home_index.intersects(search_rect)

            for home_id in candidate_ids:
                home = home_by_id.get(home_id)
                if home is None:
                    continue
                home_pt = self._point_from_geom(home["geom"])
                d = self._distance_between_points_meters(crs, ind_pt, home_pt)
                if d <= float(radius_m):
                    violations.append(
                        {
                            "industrial_id": ind["id"],
                            "industrial_feature": ind["feature"],
                            "home_id": home["id"],
                            "home_feature": home["feature"],
                            "class": hazard_class,
                            "radius": radius_m,
                            "distance_m": float(d),
                        }
                    )

        return violations, szz_zones
