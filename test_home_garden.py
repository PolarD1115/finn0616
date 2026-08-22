"""
test_home_garden.py — Phase 4 种植/库存/烹饪测试
===================================================
覆盖：
- service 参数校验（种植/浇水/收获/烹饪/食用/喂食）
- 幂等逻辑（action_key 重复返回 ACTION_EXISTS）
- 数据库不可用降级
- 安全（不泄露敏感信息）
- 回归（旧测试不受影响）
"""

import unittest
from unittest.mock import patch, MagicMock

from home import service as svc


# ============================================================
# 1. 种植参数校验
# ============================================================

class Test01PlantSeedValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.plant_seed("ai_primary", "tomato", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_empty_actor(self):
        result = svc.plant_seed("", "tomato", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_MEMBER_KEY")

    def test_empty_seed(self):
        result = svc.plant_seed("ai_primary", "", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_SEED_KEY")

    @patch("home.repository.rpc_plant_seed")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "status": "succeeded", "plant_id": "p1"}
        result = svc.plant_seed("ai_primary", "tomato", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 2. 浇水参数校验
# ============================================================

class Test02WaterPlantValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.water_plant("ai_primary", "plant-uuid", "")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_ACTION_KEY")

    def test_empty_plant_id(self):
        result = svc.water_plant("ai_primary", "", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_PLANT_ID")

    @patch("home.repository.rpc_water_plant")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.water_plant("ai_primary", "plant-uuid", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 3. 收获参数校验
# ============================================================

class Test03HarvestValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.harvest_plant("ai_primary", "plant-uuid", "")
        self.assertFalse(result["ok"])

    def test_empty_plant_id(self):
        result = svc.harvest_plant("ai_primary", "", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_harvest_plant")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "yield": 3}
        result = svc.harvest_plant("ai_primary", "plant-uuid", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 4. 烹饪参数校验
# ============================================================

class Test04CookRecipeValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.cook_recipe("ai_primary", "tomato_egg", "")
        self.assertFalse(result["ok"])

    def test_empty_recipe(self):
        result = svc.cook_recipe("ai_primary", "", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_cook_recipe")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "dish_id": "d1"}
        result = svc.cook_recipe("ai_primary", "tomato_egg", "act1")
        self.assertTrue(result["ok"])


class Test05CookFreestyleValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.cook_freestyle("ai_primary", {"tomato": 2}, "")
        self.assertFalse(result["ok"])

    def test_empty_ingredients(self):
        result = svc.cook_freestyle("ai_primary", {}, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "EMPTY_INGREDIENTS")

    def test_none_ingredients(self):
        result = svc.cook_freestyle("ai_primary", None, "act1")
        self.assertFalse(result["ok"])

    def test_too_many_types(self):
        result = svc.cook_freestyle("ai_primary", {"a": 1, "b": 1, "c": 1, "d": 1, "e": 1, "f": 1}, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "TOO_MANY_INGREDIENT_TYPES")

    def test_invalid_quantity(self):
        result = svc.cook_freestyle("ai_primary", {"tomato": 0}, "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "INVALID_QUANTITY")

    def test_negative_quantity(self):
        result = svc.cook_freestyle("ai_primary", {"tomato": -1}, "act1")
        self.assertFalse(result["ok"])

    def test_non_int_quantity(self):
        result = svc.cook_freestyle("ai_primary", {"tomato": "abc"}, "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_cook_freestyle")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.cook_freestyle("ai_primary", {"tomato": 2, "egg": 1}, "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 5. 食用和喂食参数校验
# ============================================================

class Test06EatDishValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.eat_dish("ai_primary", "dish-uuid", "")
        self.assertFalse(result["ok"])

    def test_empty_dish_id(self):
        result = svc.eat_dish("ai_primary", "", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_eat_dish")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.eat_dish("ai_primary", "dish-uuid", "act1")
        self.assertTrue(result["ok"])


class Test07FeedMemberValidation(unittest.TestCase):
    def test_empty_action_key(self):
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "")
        self.assertFalse(result["ok"])

    def test_empty_target(self):
        result = svc.feed_member("ai_primary", "", "dish-uuid", "act1")
        self.assertFalse(result["ok"])

    def test_empty_dish(self):
        result = svc.feed_member("ai_primary", "pet_xiaoman", "", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_feed_member")
    def test_valid(self, mock_rpc):
        mock_rpc.return_value = {"ok": True}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "dish-uuid", "act1")
        self.assertTrue(result["ok"])


# ============================================================
# 8. 幂等测试（RPC 返回 ACTION_EXISTS）
# ============================================================

class Test08Idempotency(unittest.TestCase):
    @patch("home.repository.rpc_plant_seed")
    def test_duplicate_plant(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.plant_seed("ai_primary", "tomato", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "ACTION_EXISTS")

    @patch("home.repository.rpc_water_plant")
    def test_duplicate_water(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.water_plant("ai_primary", "p1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_harvest_plant")
    def test_duplicate_harvest(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.harvest_plant("ai_primary", "p1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_cook_recipe")
    def test_duplicate_cook(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.cook_recipe("ai_primary", "tomato_egg", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_eat_dish")
    def test_duplicate_eat(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.eat_dish("ai_primary", "d1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_feed_member")
    def test_duplicate_feed(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ACTION_EXISTS"}
        result = svc.feed_member("ai_primary", "pet_xiaoman", "d1", "act1")
        self.assertFalse(result["ok"])


# ============================================================
# 9. RPC 错误码透传
# ============================================================

class Test09ErrorCodes(unittest.TestCase):
    @patch("home.repository.rpc_harvest_plant")
    def test_not_mature(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "NOT_MATURE"}
        result = svc.harvest_plant("ai_primary", "p1", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "NOT_MATURE")

    @patch("home.repository.rpc_harvest_plant")
    def test_already_harvested(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "ALREADY_HARVESTED"}
        result = svc.harvest_plant("ai_primary", "p1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_cook_recipe")
    def test_insufficient_ingredients(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "INSUFFICIENT_INGREDIENTS"}
        result = svc.cook_recipe("ai_primary", "tomato_egg", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_eat_dish")
    def test_no_servings(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "NO_SERVINGS"}
        result = svc.eat_dish("ai_primary", "d1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_feed_member")
    def test_self_target(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "SELF_TARGET"}
        result = svc.feed_member("ai_primary", "ai_primary", "d1", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_plant_seed")
    def test_seed_not_found(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "SEED_NOT_FOUND"}
        result = svc.plant_seed("ai_primary", "nonexistent", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository.rpc_cook_recipe")
    def test_recipe_not_found(self, mock_rpc):
        mock_rpc.return_value = {"ok": False, "error_code": "RECIPE_NOT_FOUND"}
        result = svc.cook_recipe("ai_primary", "nonexistent", "act1")
        self.assertFalse(result["ok"])


# ============================================================
# 10. 观察接口
# ============================================================

class Test10Observe(unittest.TestCase):
    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_seed_catalog")
    @patch("home.repository.fetch_recent_events")
    def test_garden_observe(self, m_events, m_seeds, m_plants):
        m_plants.return_value = [
            {"id": "p1", "name": "番茄", "seed_key": "tomato", "stage": "growing",
             "health": 80, "water_level": 50, "status": "active", "planted_at": "2026-08-18T10:00:00Z"}
        ]
        m_seeds.return_value = [
            {"stable_key": "tomato", "name": "番茄", "emoji": "🍅"}
        ]
        m_events.return_value = []
        result = svc.garden_observe()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["plants"]), 1)
        self.assertEqual(result["data"]["plants"][0]["name"], "番茄")
        self.assertFalse(result["data"]["plants"][0]["is_mature"])
        self.assertEqual(len(result["data"]["available_seeds"]), 1)

    @patch("home.repository.fetch_inventory")
    @patch("home.repository.fetch_recipe_catalog")
    @patch("home.repository.fetch_dishes")
    def test_pantry_observe(self, m_dishes, m_recipes, m_inv):
        m_inv.return_value = [
            {"item_key": "tomato", "item_kind": "ingredient", "storage_location": "garden_storage", "quantity": 3, "unit": "个"}
        ]
        m_recipes.return_value = [
            {"stable_key": "tomato_egg", "name": "番茄炒蛋", "emoji": "🍳"}
        ]
        m_dishes.return_value = [
            {"id": "d1", "name": "番茄炒蛋", "servings": 2, "quality": 70}
        ]
        result = svc.pantry_observe()
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["data"]["inventory"]), 1)
        self.assertEqual(len(result["data"]["dishes"]), 1)
        self.assertEqual(len(result["data"]["available_recipes"]), 1)


# ============================================================
# 11. 数据库不可用降级
# ============================================================

class Test11DBUnavailable(unittest.TestCase):
    @patch("home.repository._get_supabase")
    def test_plant_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.plant_seed("ai_primary", "tomato", "act1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "DB_UNAVAILABLE")

    @patch("home.repository._get_supabase")
    def test_cook_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.cook_recipe("ai_primary", "tomato_egg", "act1")
        self.assertFalse(result["ok"])

    @patch("home.repository._get_supabase")
    def test_garden_observe_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.garden_observe()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["plants"], [])

    @patch("home.repository._get_supabase")
    def test_pantry_observe_no_db(self, mock_sb):
        mock_sb.return_value = None
        result = svc.pantry_observe()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["inventory"], [])


# ============================================================
# 12. 安全测试
# ============================================================

class Test12Security(unittest.TestCase):
    @patch("home.repository.rpc_plant_seed")
    def test_plant_result_no_secrets(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "plant_name": "番茄", "narrative_facts": ["种下了番茄"]}
        result = svc.plant_seed("ai_primary", "tomato", "act1")
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())

    @patch("home.repository.rpc_cook_recipe")
    def test_cook_result_no_secrets(self, mock_rpc):
        mock_rpc.return_value = {"ok": True, "dish_name": "番茄炒蛋", "servings": 2}
        result = svc.cook_recipe("ai_primary", "tomato_egg", "act1")
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())

    @patch("home.repository.fetch_plants")
    @patch("home.repository.fetch_seed_catalog")
    @patch("home.repository.fetch_recent_events")
    def test_garden_observe_no_secrets(self, m_ev, m_seeds, m_plants):
        m_plants.return_value = []
        m_seeds.return_value = []
        m_ev.return_value = []
        result = svc.garden_observe()
        result_str = str(result)
        for kw in ("api_key", "token", "password", "secret", "cookie"):
            self.assertNotIn(kw, result_str.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
