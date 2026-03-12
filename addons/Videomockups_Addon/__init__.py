bl_info = {
    "name": "Videomockup Outputs",
    "author": "Dan",
    "version": (1, 0, 0),
    "blender": (4, 3, 2),
    "location": "Node Editor > Sidebar > Videomockup",
    "description": "Set up compositing nodes for video mockup outputs",
    "category": "Node",
}

import bpy
import os
import bpy.types
from bpy_extras.io_utils import ExportHelper
from bpy.types import Operator, Panel
from bpy.props import StringProperty, EnumProperty

class VIDEOMOCKUP_OT_toggle_highlight_curve(bpy.types.Operator):
    bl_idname = "videomockup.toggle_highlight_curve"
    bl_label = "Toggle Highlight Curve"
    bl_description = "Toggle the highlight curve between normal and flat bottom"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        try:
            current_state = context.scene.videomockup_curves.highlight_curve_toggle
            new_state = not current_state
            
            # Store the current property state before changing it
            context.scene.videomockup_curves.highlight_curve_toggle = new_state
            
            group_name = "All Previews"
            if group_name not in bpy.data.node_groups:
                self.report({'ERROR'}, f"Node group '{group_name}' not found!")
                return {'CANCELLED'}
            
            group = bpy.data.node_groups[group_name]
            node_name = "RGB Curves Highlight"
            node = group.nodes.get(node_name)
            if not node:
                self.report({'ERROR'}, f"Node '{node_name}' not found!")
                return {'CANCELLED'}
            
            # We'll create a new node each time, which avoids removing curve points
            new_node = group.nodes.new('CompositorNodeCurveRGB')
            new_node.name = "RGB Curves Highlight (New)"
            new_node.label = "RGB Curves Highlight"
            new_node.width = node.width
            new_node.location = node.location
            
            # When turning OFF
            if current_state:
                # Save the current curve points to scene property
                if "highlight_points" not in context.scene:
                    context.scene["highlight_points"] = []
                
                # Store just the point locations
                points = []
                for p in node.mapping.curves[3].points:
                    points.append((p.location.x, p.location.y))
                
                context.scene["highlight_points"] = points
                print(f"Stored {len(points)} highlight curve points")
                
                # Set up a flat bottom curve in the new node
                curve = new_node.mapping.curves[3]
                if len(curve.points) >= 2:  # We should have at least 2 points by default
                    curve.points[0].location = (0.0, 0.0)
                    curve.points[1].location = (1.0, 0.0)
            
            # When turning ON
            else:
                # Set up a curve based on stored points, or use default diagonal
                curve = new_node.mapping.curves[3]
                
                if "highlight_points" in context.scene and context.scene["highlight_points"]:
                    stored_points = context.scene["highlight_points"]
                    
                    # Make sure we have at least 2 points to work with
                    if len(stored_points) >= 2:
                        # Set the existing points
                        curve.points[0].location = stored_points[0]
                        curve.points[1].location = stored_points[1]
                        
                        # Add any additional points
                        for i in range(2, len(stored_points)):
                            try:
                                curve.points.new(stored_points[i][0], stored_points[i][1])
                            except Exception as e:
                                print(f"Error adding point {i}: {e}")
                        
                        print(f"Restored {len(stored_points)} highlight curve points")
                    else:
                        # Not enough points, use default diagonal
                        curve.points[0].location = (0.0, 0.0)
                        curve.points[1].location = (1.0, 1.0)
                else:
                    # No stored points, use default diagonal
                    curve.points[0].location = (0.0, 0.0)
                    curve.points[1].location = (1.0, 1.0)
            
            # Update the mapping
            new_node.mapping.update()
            
            # Copy all connections
            # Input connections
            for link in list(group.links):
                if link.to_node == node:
                    src_socket = link.from_socket
                    dst_socket_name = link.to_socket.name
                    dst_socket = new_node.inputs.get(dst_socket_name)
                    
                    if dst_socket:
                        group.links.new(src_socket, dst_socket)
            
            # Output connections
            for link in list(group.links):
                if link.from_node == node:
                    src_socket_name = link.from_socket.name
                    dst_node = link.to_node
                    dst_socket = link.to_socket
                    
                    src_socket = new_node.outputs.get(src_socket_name)
                    if src_socket:
                        group.links.remove(link)
                        group.links.new(src_socket, dst_socket)
            
            # Remove the old node
            group.nodes.remove(node)
            
            # Rename new node to the standard name
            new_node.name = "RGB Curves Highlight"
            
            # Force refresh of viewer
            self.force_viewer_update(context)
            
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to toggle highlight curve: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def force_viewer_update(self, context):
        # First force a redraw of all areas
        for area in context.screen.areas:
            area.tag_redraw()
            
        # Force refresh of Viewer node by toggling the output
        current_output = context.scene.videomockup_selected_output
        
        # Toggle viewer output temporarily to force update
        temp_output = "RGB" if current_output != "RGB" else "Image Render"
        bpy.ops.videomockup.switch_output(output_name=temp_output)
        bpy.ops.videomockup.switch_output(output_name=current_output)
        
        # Force backdrop refresh in all NODE_EDITOR areas
        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    if space.type == 'NODE_EDITOR':
                        # Toggle backdrop to force refresh
                        current_state = space.show_backdrop
                        space.show_backdrop = not current_state
                        space.show_backdrop = current_state
            elif area.type == 'IMAGE_EDITOR':
                area.tag_redraw()
                
            # Keep refreshing all areas just to be safe
            area.tag_redraw()


class VIDEOMOCKUP_OT_toggle_shadow_curve(bpy.types.Operator):
    bl_idname = "videomockup.toggle_shadow_curve"
    bl_label = "Toggle Shadow Curve"
    bl_description = "Toggle the shadow curve between normal and flat top"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        try:
            current_state = context.scene.videomockup_curves.shadow_curve_toggle
            new_state = not current_state
            
            # Store the current property state before changing it
            context.scene.videomockup_curves.shadow_curve_toggle = new_state
            
            group_name = "All Previews"
            if group_name not in bpy.data.node_groups:
                self.report({'ERROR'}, f"Node group '{group_name}' not found!")
                return {'CANCELLED'}
            
            group = bpy.data.node_groups[group_name]
            node_name = "RGB Curves Shadow"
            node = group.nodes.get(node_name)
            if not node:
                self.report({'ERROR'}, f"Node '{node_name}' not found!")
                return {'CANCELLED'}
            
            # We'll create a new node each time, which avoids removing curve points
            new_node = group.nodes.new('CompositorNodeCurveRGB')
            new_node.name = "RGB Curves Shadow (New)"
            new_node.label = "RGB Curves Shadow"
            new_node.width = node.width
            new_node.location = node.location
            
            # When turning OFF
            if current_state:
                # Save the current curve points to scene property
                if "shadow_points" not in context.scene:
                    context.scene["shadow_points"] = []
                
                # Store just the point locations
                points = []
                for p in node.mapping.curves[3].points:
                    points.append((p.location.x, p.location.y))
                
                context.scene["shadow_points"] = points
                print(f"Stored {len(points)} shadow curve points")
                
                # Set up a flat top curve in the new node
                curve = new_node.mapping.curves[3]
                if len(curve.points) >= 2:  # We should have at least 2 points by default
                    curve.points[0].location = (0.0, 1.0)
                    curve.points[1].location = (1.0, 1.0)
            
            # When turning ON
            else:
                # Set up a curve based on stored points, or use default diagonal
                curve = new_node.mapping.curves[3]
                
                if "shadow_points" in context.scene and context.scene["shadow_points"]:
                    stored_points = context.scene["shadow_points"]
                    
                    # Make sure we have at least 2 points to work with
                    if len(stored_points) >= 2:
                        # Set the existing points
                        curve.points[0].location = stored_points[0]
                        curve.points[1].location = stored_points[1]
                        
                        # Add any additional points
                        for i in range(2, len(stored_points)):
                            try:
                                curve.points.new(stored_points[i][0], stored_points[i][1])
                            except Exception as e:
                                print(f"Error adding point {i}: {e}")
                        
                        print(f"Restored {len(stored_points)} shadow curve points")
                    else:
                        # Not enough points, use default diagonal
                        curve.points[0].location = (0.0, 0.0)
                        curve.points[1].location = (1.0, 1.0)
                else:
                    # No stored points, use default diagonal
                    curve.points[0].location = (0.0, 0.0)
                    curve.points[1].location = (1.0, 1.0)
            
            # Update the mapping
            new_node.mapping.update()
            
            # Copy all connections
            # Input connections
            for link in list(group.links):
                if link.to_node == node:
                    src_socket = link.from_socket
                    dst_socket_name = link.to_socket.name
                    dst_socket = new_node.inputs.get(dst_socket_name)
                    
                    if dst_socket:
                        group.links.new(src_socket, dst_socket)
            
            # Output connections
            for link in list(group.links):
                if link.from_node == node:
                    src_socket_name = link.from_socket.name
                    dst_node = link.to_node
                    dst_socket = link.to_socket
                    
                    src_socket = new_node.outputs.get(src_socket_name)
                    if src_socket:
                        group.links.remove(link)
                        group.links.new(src_socket, dst_socket)
            
            # Remove the old node
            group.nodes.remove(node)
            
            # Rename new node to the standard name
            new_node.name = "RGB Curves Shadow"
            
            # Force refresh of viewer
            self.force_viewer_update(context)
            
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to toggle shadow curve: {str(e)}")
            import traceback
            traceback.print_exc()
            return {'CANCELLED'}
    
    def force_viewer_update(self, context):
        # First force a redraw of all areas
        for area in context.screen.areas:
            area.tag_redraw()
            
        # Force refresh of Viewer node by toggling the output
        current_output = context.scene.videomockup_selected_output
        
        # Toggle viewer output temporarily to force update
        temp_output = "RGB" if current_output != "RGB" else "Image Render"
        bpy.ops.videomockup.switch_output(output_name=temp_output)
        bpy.ops.videomockup.switch_output(output_name=current_output)
        
        # Force backdrop refresh in all NODE_EDITOR areas
        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    if space.type == 'NODE_EDITOR':
                        # Toggle backdrop to force refresh
                        current_state = space.show_backdrop
                        space.show_backdrop = not current_state
                        space.show_backdrop = current_state
            elif area.type == 'IMAGE_EDITOR':
                area.tag_redraw()
                
            # Keep refreshing all areas just to be safe
            area.tag_redraw()
        
def update_highlight_curve(self, context):
    print("Highlight curve update called")  # Debug
    try:
        # Be more explicit about finding the node group
        group_name = "All Previews"
        if group_name not in bpy.data.node_groups:
            print(f"Node group '{group_name}' not found!")
            return
        
        group = bpy.data.node_groups[group_name]
        node_name = "RGB Curves Highlight"
        node = group.nodes.get(node_name)
        if not node:
            print(f"Node '{node_name}' not found in group '{group_name}'!")
            # Print all node names in the group for debugging
            print(f"Available nodes: {[n.name for n in group.nodes]}")
            return
            
        # Verify that the node has the properties we need
        if not hasattr(node, "mapping") or not hasattr(node.mapping, "curves"):
            print(f"Node '{node_name}' doesn't have mapping.curves property!")
            return
            
        curve = node.mapping.curves[3]
        
        # Check current points in curve
        print(f"Current curve points before clearing: {len(curve.points)}")
        for i, p in enumerate(curve.points):
            print(f"  Point {i}: ({p.location[0]}, {p.location[1]})")
            
        curve.points.clear()
        
        # Determine toggle state
        toggle_state = False
        if hasattr(context.scene, 'videomockup_curves') and hasattr(context.scene.videomockup_curves, 'highlight_curve_toggle'):
            toggle_state = context.scene.videomockup_curves.highlight_curve_toggle
        elif hasattr(context.scene, 'videomockup_highlight_curve_toggle'):
            toggle_state = context.scene.videomockup_highlight_curve_toggle
        elif hasattr(self, 'highlight_curve_toggle'):
            toggle_state = self.highlight_curve_toggle
            
        print(f"Highlight toggle state: {toggle_state}")  # Debug
        
        if toggle_state:
            # ON state - diagonal curve
            curve.points.new(0.0, 0.0)
            curve.points.new(1.0, 1.0)
            print("Added diagonal curve (ON state)")
        else:
            # OFF state - flat bottom curve
            curve.points.new(0.0, 0.0)
            curve.points.new(1.0, 0.0)
            print("Added flat bottom curve (OFF state)")

        node.mapping.update()
        print("Updated curve mapping")
        
        # Force UI update
        for area in bpy.context.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()
                print("Tagged NODE_EDITOR for redraw")
                
        # Force viewer update by toggling outputs
        current_output = context.scene.videomockup_selected_output
        if current_output:
            temp_output = "RGB" if current_output != "RGB" else "Image Render"
            # Only run these operators if they're registered and we're in a context where they can run
            if hasattr(bpy.ops.videomockup, 'switch_output'):
                # Try to get a context that can run the operator
                override = context.copy()
                try:
                    # Switch to a different output and back to force update
                    bpy.ops.videomockup.switch_output(override, output_name=temp_output)
                    bpy.ops.videomockup.switch_output(override, output_name=current_output)
                    print(f"Forced viewer update by toggling outputs: {temp_output} -> {current_output}")
                except Exception as e:
                    print(f"Could not toggle outputs: {e}")
    
    except Exception as e:
        print(f"Highlight curve update failed: {e}")
        import traceback
        traceback.print_exc()

def update_shadow_curve(self, context):
    print("Shadow curve update called")  # Debug
    try:
        # Be more explicit about finding the node group
        group_name = "All Previews"
        if group_name not in bpy.data.node_groups:
            print(f"Node group '{group_name}' not found!")
            return
        
        group = bpy.data.node_groups[group_name]
        node_name = "RGB Curves Shadow"
        node = group.nodes.get(node_name)
        if not node:
            print(f"Node '{node_name}' not found in group '{group_name}'!")
            # Print all node names in the group for debugging
            print(f"Available nodes: {[n.name for n in group.nodes]}")
            return
            
        # Verify that the node has the properties we need
        if not hasattr(node, "mapping") or not hasattr(node.mapping, "curves"):
            print(f"Node '{node_name}' doesn't have mapping.curves property!")
            return
            
        curve = node.mapping.curves[3]
        
        # Check current points in curve
        print(f"Current curve points before clearing: {len(curve.points)}")
        for i, p in enumerate(curve.points):
            print(f"  Point {i}: ({p.location[0]}, {p.location[1]})")
            
        curve.points.clear()
        
        # Determine toggle state
        toggle_state = False
        if hasattr(context.scene, 'videomockup_curves') and hasattr(context.scene.videomockup_curves, 'shadow_curve_toggle'):
            toggle_state = context.scene.videomockup_curves.shadow_curve_toggle
        elif hasattr(context.scene, 'videomockup_shadow_curve_toggle'):
            toggle_state = context.scene.videomockup_shadow_curve_toggle
        elif hasattr(self, 'shadow_curve_toggle'):
            toggle_state = self.shadow_curve_toggle
            
        print(f"Shadow toggle state: {toggle_state}")  # Debug
        
        if toggle_state:
            # ON state - diagonal curve
            curve.points.new(0.0, 0.0)
            curve.points.new(1.0, 1.0)
            print("Added diagonal curve (ON state)")
        else:
            # OFF state - flat top curve
            curve.points.new(0.0, 1.0)
            curve.points.new(1.0, 1.0)
            print("Added flat top curve (OFF state)")

        node.mapping.update()
        print("Updated curve mapping")
        
        # Force UI update
        for area in bpy.context.screen.areas:
            if area.type == 'NODE_EDITOR':
                area.tag_redraw()
                print("Tagged NODE_EDITOR for redraw")
                
        # Force viewer update by toggling outputs
        current_output = context.scene.videomockup_selected_output
        if current_output:
            temp_output = "RGB" if current_output != "RGB" else "Image Render"
            # Only run these operators if they're registered and we're in a context where they can run
            if hasattr(bpy.ops.videomockup, 'switch_output'):
                # Try to get a context that can run the operator
                override = context.copy()
                try:
                    # Switch to a different output and back to force update
                    bpy.ops.videomockup.switch_output(override, output_name=temp_output)
                    bpy.ops.videomockup.switch_output(override, output_name=current_output)
                    print(f"Forced viewer update by toggling outputs: {temp_output} -> {current_output}")
                except Exception as e:
                    print(f"Could not toggle outputs: {e}")
    
    except Exception as e:
        print(f"Shadow curve update failed: {e}")
        import traceback
        traceback.print_exc()

def update_placeholder_color(self, context):
    # Get the All Previews group
    all_preview_group = bpy.data.node_groups.get("All Previews")
    if not all_preview_group:
        return
        
    # Get the Highlight/Shadow Preview node
    hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
    if not hs_node or not hs_node.node_tree:
        return
        
    # Get the H/S Preview node tree
    hs_group = hs_node.node_tree
    
    # Get the RGB node and set its color
    rgb_node = hs_group.nodes.get("RGB")
    if rgb_node:
        rgb_node.outputs[0].default_value = (
            self.placeholder_color[0],
            self.placeholder_color[1],
            self.placeholder_color[2],
            1.0
        )
    
    # Get the Mix nodes for highlight and shadow
    mix_highlight = hs_group.nodes.get("Mix")
    mix_shadow = hs_group.nodes.get("Mix.001")
    
    if not mix_highlight or not mix_shadow:
        return
    
    # Set the color for both nodes if they're not linked
    color = self.placeholder_color
    
    if not mix_highlight.inputs[2].is_linked:
        mix_highlight.inputs[2].default_value = (color[0], color[1], color[2], 1.0)
        
    if not mix_shadow.inputs[2].is_linked:
        mix_shadow.inputs[2].default_value = (color[0], color[1], color[2], 1.0)
    
    # If placeholder is enabled and using custom color, update the Alpha Over PH connection
    if self.placeholder_enabled and self.placeholder_use_custom_color:
        alpha_over_ph = hs_group.nodes.get("Alpha Over PH")  # Updated to use Alpha Over PH
        if alpha_over_ph:
            # First remove any existing connection
            for link in list(hs_group.links):
                if link.to_node == alpha_over_ph and link.to_socket == alpha_over_ph.inputs[2]:
                    hs_group.links.remove(link)
            
            # Connect the RGB node to Alpha Over PH
            hs_group.links.new(rgb_node.outputs["RGBA"], alpha_over_ph.inputs[2])

def update_placeholder_use_custom_color(self, context):
    # If turning on custom color, turn off image
    if self.placeholder_use_custom_color:
        self.placeholder_use_image = False
    # If trying to turn off custom color while image is also off, prevent it
    elif not self.placeholder_use_image:
        # Set it back to True since we can't have both off
        self.placeholder_use_custom_color = True
        return
    
    # Only update if placeholder is enabled
    if self.placeholder_enabled:
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            return
            
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        if not hs_node or not hs_node.node_tree:
            return
            
        hs_group = hs_node.node_tree
        alpha_over_ph = hs_group.nodes.get("Alpha Over PH")  # Updated to use Alpha Over PH
        rgb_node = hs_group.nodes.get("RGB")
        group_input = next((n for n in hs_group.nodes if n.bl_idname == 'NodeGroupInput'), None)
        
        if not (alpha_over_ph and rgb_node and group_input):
            return
            
        # Remove existing connection to Alpha Over PH second input (input[2])
        for link in list(hs_group.links):
            if link.to_node == alpha_over_ph and link.to_socket == alpha_over_ph.inputs[2]:
                hs_group.links.remove(link)
        
        if self.placeholder_use_custom_color:
            # Set the RGB node color
            rgb_node.outputs[0].default_value = (
                self.placeholder_color[0],
                self.placeholder_color[1],
                self.placeholder_color[2],
                1.0
            )
            # Connect RGB node to Alpha Over PH
            hs_group.links.new(rgb_node.outputs["RGBA"], alpha_over_ph.inputs[2])
        else:
            # If not using custom color, connect DiffCol
            hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_ph.inputs[2])

def update_placeholder_use_image(self, context):
    # If turning on image, turn off custom color
    if self.placeholder_use_image:
        self.placeholder_use_custom_color = False
    # If trying to turn off image while custom color is also off, prevent it
    elif not self.placeholder_use_custom_color:
        # Set it back to True since we can't have both off
        self.placeholder_use_image = True
        return

    # Update connections if placeholder is enabled
    if self.placeholder_enabled:
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            return
            
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        if not hs_node or not hs_node.node_tree:
            return
            
        hs_group = hs_node.node_tree
        alpha_over_ph = hs_group.nodes.get("Alpha Over PH")  # Updated to use Alpha Over PH
        group_input = next((n for n in hs_group.nodes if n.bl_idname == 'NodeGroupInput'), None)
        transform_node = hs_group.nodes.get("Transform")
        
        if not (alpha_over_ph and group_input and transform_node):
            return
            
        # Remove existing connection to Alpha Over PH second input (input[2])
        for link in list(hs_group.links):
            if link.to_node == alpha_over_ph and link.to_socket == alpha_over_ph.inputs[2]:
                hs_group.links.remove(link)
                
        # If using image, connect transform node
        if self.placeholder_use_image:
            hs_group.links.new(transform_node.outputs["Image"], alpha_over_ph.inputs[2])
        else:
            # Otherwise connect DiffCol
            hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_ph.inputs[2])

def update_transform_x(self, context):
    all_preview_group = bpy.data.node_groups.get("All Previews")
    if not all_preview_group:
        return
        
    hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
    if not hs_node or not hs_node.node_tree:
        return
        
    hs_group = hs_node.node_tree
    transform_node = hs_group.nodes.get("Transform")
    if not transform_node:
        return
        
    transform_node.inputs['X'].default_value = self.placeholder_transform_x

def update_transform_y(self, context):
    all_preview_group = bpy.data.node_groups.get("All Previews")
    if not all_preview_group:
        return
        
    hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
    if not hs_node or not hs_node.node_tree:
        return
        
    hs_group = hs_node.node_tree
    transform_node = hs_group.nodes.get("Transform")
    if not transform_node:
        return
        
    transform_node.inputs['Y'].default_value = self.placeholder_transform_y

def update_transform_scale(self, context):
    all_preview_group = bpy.data.node_groups.get("All Previews")
    if not all_preview_group:
        return
        
    hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
    if not hs_node or not hs_node.node_tree:
        return
        
    hs_group = hs_node.node_tree
    transform_node = hs_group.nodes.get("Transform")
    if not transform_node:
        return
        
    transform_node.inputs['Scale'].default_value = self.placeholder_transform_scale

def update_transform_angle(self, context):
    all_preview_group = bpy.data.node_groups.get("All Previews")
    if not all_preview_group:
        return
        
    hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
    if not hs_node or not hs_node.node_tree:
        return
        
    hs_group = hs_node.node_tree
    transform_node = hs_group.nodes.get("Transform")
    if not transform_node:
        return
        
    transform_node.inputs['Angle'].default_value = self.placeholder_transform_angle

def update_adjust_settings(self, context):
    try:
        # If turning on adjust settings
        if self.adjust_settings_enabled:
            # ALWAYS store the current resolution when turning ON
            self.original_resolution_x = context.scene.render.resolution_x
            self.original_resolution_y = context.scene.render.resolution_y
            self.original_resolution_stored = True
            
            # Get background image dimensions from channel 1
            seq_editor = context.scene.sequence_editor
            if not seq_editor:
                print("No sequence editor found")
                return
                
            background_seq = None
            for seq in seq_editor.sequences_all:
                if seq.channel == 1:
                    background_seq = seq
                    break
                    
            if not background_seq or background_seq.type != 'IMAGE':
                print("No background image found in channel 1")
                return
                
            # Get dimensions of the background image
            if hasattr(background_seq, 'elements') and background_seq.elements:
                element = background_seq.elements[0]
                if element.orig_width > 0 and element.orig_height > 0:
                    # Set new resolution: width = image width, height = image height * 2
                    print(f"Setting resolution to {element.orig_width}x{element.orig_height*2}")
                    context.scene.render.resolution_x = element.orig_width
                    context.scene.render.resolution_y = element.orig_height * 2
                    
                    # NOW ADJUST THE STRIP TRANSFORMS
                    # Find both strips and adjust their positions
                    adjust_strip_transforms(context)
                    
        else:
            # Restore original resolution if we have stored values
            if self.original_resolution_stored:
                print(f"Restoring resolution to {self.original_resolution_x}x{self.original_resolution_y}")
                context.scene.render.resolution_x = self.original_resolution_x
                context.scene.render.resolution_y = self.original_resolution_y
                
                # Reset strip transforms to their default positions
                reset_strip_transforms(context)
                
    except Exception as e:
        print(f"Error in update_adjust_settings: {str(e)}")
        import traceback
        traceback.print_exc()

def adjust_strip_transforms(context):
    """
    Adjust the transform of strips to align them with the edges of the frame.
    Background strip (channel 1) aligns with bottom, RGB strip (channel 2) aligns with top.
    """
    seq_editor = context.scene.sequence_editor
    if not seq_editor:
        return
    
    # Find the strips in channels 1 and 2
    background_strip = None
    rgb_strip = None
    
    for seq in seq_editor.sequences_all:
        if seq.channel == 1:
            background_strip = seq
        elif seq.channel == 2:
            rgb_strip = seq
    
    # Adjust the background strip (move to bottom)
    if background_strip:
        # For regular image strips, use transform property
        if hasattr(background_strip, 'transform'):
            # Position strip at bottom of frame
            bottom_position = -context.scene.render.resolution_y / 4
            background_strip.transform.offset_y = bottom_position
            print(f"Adjusted background strip to y={bottom_position}")
        else:
            # Handle non-transform strips by creating a transform strip
            # First, select the strip
            bpy.ops.sequencer.select_all(action='DESELECT')
            background_strip.select = True
            context.scene.sequence_editor.active_strip = background_strip
            
            # Add a transform effect
            bpy.ops.sequencer.effect_strip_add(type='TRANSFORM')
            
            # Find the newly created transform strip
            for seq in seq_editor.sequences_all:
                if seq.type == 'TRANSFORM' and hasattr(seq, 'input_1') and seq.input_1 == background_strip:
                    # Position the transform strip at the bottom
                    bottom_position = -context.scene.render.resolution_y / 4
                    if hasattr(seq, 'translate_start_y'):
                        seq.translate_start_y = bottom_position
                    print(f"Created and adjusted transform strip for background to y={bottom_position}")
                    break
    
    # Adjust the RGB strip (move to top)
    if rgb_strip:
        # For regular image strips, use transform property
        if hasattr(rgb_strip, 'transform'):
            # Position strip at top of frame
            top_position = context.scene.render.resolution_y / 4
            rgb_strip.transform.offset_y = top_position
            print(f"Adjusted RGB strip to y={top_position}")
        else:
            # Handle non-transform strips by creating a transform strip
            # First, select the strip
            bpy.ops.sequencer.select_all(action='DESELECT')
            rgb_strip.select = True
            context.scene.sequence_editor.active_strip = rgb_strip
            
            # Add a transform effect
            bpy.ops.sequencer.effect_strip_add(type='TRANSFORM')
            
            # Find the newly created transform strip
            for seq in seq_editor.sequences_all:
                if seq.type == 'TRANSFORM' and hasattr(seq, 'input_1') and seq.input_1 == rgb_strip:
                    # Position the transform strip at the top
                    top_position = context.scene.render.resolution_y / 4
                    if hasattr(seq, 'translate_start_y'):
                        seq.translate_start_y = top_position
                    print(f"Created and adjusted transform strip for RGB to y={top_position}")
                    break

def reset_strip_transforms(context):
    """Reset transform of strips to their default positions"""
    seq_editor = context.scene.sequence_editor
    if not seq_editor:
        return
    
    # Reset all transform strips to their default positions
    for seq in seq_editor.sequences_all:
        if seq.type == 'TRANSFORM' and hasattr(seq, 'translate_start_y'):
            seq.translate_start_y = 0
            print(f"Reset transform strip {seq.name} to default position")
        elif hasattr(seq, 'transform') and hasattr(seq.transform, 'offset_y'):
            seq.transform.offset_y = 0
            print(f"Reset strip {seq.name} transform to default position")

class VideomockupCurveSettings(bpy.types.PropertyGroup):
    highlight_curve_toggle: bpy.props.BoolProperty(
        name="Highlight Curve Mode",
        default=True,  # Default is ON
        description="Toggle the highlight curve mode",
        update=update_highlight_curve
    )
    
    shadow_curve_toggle: bpy.props.BoolProperty(
        name="Shadow Curve Mode",
        default=True,  # Default is ON
        description="Toggle the shadow curve mode",
        update=update_shadow_curve
    )

    placeholder_enabled: bpy.props.BoolProperty(
        name="Enable Placeholder Overlay",
        default=False,
        description="Toggle the placeholder overlay"
    )

    placeholder_color: bpy.props.FloatVectorProperty(
        name="Placeholder Color",
        subtype='COLOR',
        default=(1.0, 1.0, 1.0),
        min=0.0,
        max=1.0,
        description="Color to use as overlay when placeholder is active",
        update=update_placeholder_color
    )

    placeholder_use_custom_color: bpy.props.BoolProperty(
        name="Custom Color",
        default=True,
        description="Enable to choose a custom color when placeholder is active",
        update=update_placeholder_use_custom_color
    )

    placeholder_use_image: bpy.props.BoolProperty(
        name="Placeholder Image",
        default=False,
        description="Enable to show image instead of color as overlay",
        update=update_placeholder_use_image
    )

    placeholder_transform_x: bpy.props.FloatProperty(
        name="X Position",
        default=0.0,
        precision=3,  # Matches Blender's default display
        description="X position of the placeholder image",
        update=update_transform_x
    )

    placeholder_transform_y: bpy.props.FloatProperty(
        name="Y Position",
        default=0.0,
        precision=3,  # Matches Blender's default display
        description="Y position of the placeholder image",
        update=update_transform_y
    )

    placeholder_transform_scale: bpy.props.FloatProperty(
        name="Scale",
        default=1.0,
        min=0.001,
        precision=3,  # Higher precision for display
        soft_min=0.01,
        soft_max=2.0,  # Define a reasonable range for UI sliders
        step=0.001,     # MUCH smaller step size for less sensitivity (was 1)
        subtype='FACTOR',  # Use FACTOR subtype for better UI representation
        description="Scale of the placeholder image",
        update=update_transform_scale
    )

    placeholder_transform_angle: bpy.props.FloatProperty(
        name="Angle",
        subtype='ANGLE',
        default=0.0,
        precision=2,  # Matches Blender's default angular display
        description="Rotation angle of the placeholder image",
        update=update_transform_angle
    )

    show_outputs: bpy.props.BoolProperty(
        name="Video Output Layers",
        default=False
    )

    show_vse_import: bpy.props.BoolProperty(
        name="Stacking Video",
        default=False
    )

    adjust_settings_enabled: bpy.props.BoolProperty(
        name="Adjust Settings",
        default=False,
        description="Automatically adjust resolution based on imported background images",
        update=update_adjust_settings  # Add this update callback
    )

    original_resolution_x: bpy.props.IntProperty(default=0)
    original_resolution_y: bpy.props.IntProperty(default=0)
    original_resolution_stored: bpy.props.BoolProperty(default=False)
    
    mp4_output_path: bpy.props.StringProperty(
        name="Output Path",
        default="//output.mp4",
        subtype='FILE_PATH',
        description="Path for MP4 output file"
    )
    
    mp4_quality: bpy.props.EnumProperty(
        name="MP4 Quality",
        description="Set the quality level for MP4 export",
        items=[
            ('HIGH', "High", "High quality"),
            ('MEDIUM', "Medium", "Medium quality"),
            ('LOW', "Low", "Low quality, smaller file size"),
        ],
        default='HIGH'
    )
    
    def initialize_transform_values(self, context):
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            return
        
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        if not hs_node or not hs_node.node_tree:
            return
            
        hs_group = hs_node.node_tree
        transform_node = hs_group.nodes.get("Transform")
        if not transform_node:
            return
        
        # Temporarily disable updates
        self._updating_transform = True
        
        # Set the property values from the node inputs
        self.placeholder_transform_x = transform_node.inputs['X'].default_value
        self.placeholder_transform_y = transform_node.inputs['Y'].default_value
        self.placeholder_transform_scale = transform_node.inputs['Scale'].default_value
        self.placeholder_transform_angle = transform_node.inputs['Angle'].default_value
        
        # Re-enable updates
        self._updating_transform = False
        
        show_outputs: bpy.props.BoolProperty(
            name="Video Output Layers",
            default=True
        )

        show_vse_import: bpy.props.BoolProperty(
            name="Stacking Video",
            default=False
        )
        
        adjust_settings_enabled: bpy.props.BoolProperty(
            name="Adjust Settings",
            default=False,
            description="Automatically adjust resolution based on imported background images",
            update=update_adjust_settings
        )

        original_resolution_x: bpy.props.IntProperty(default=0)
        original_resolution_y: bpy.props.IntProperty(default=0)
        original_resolution_stored: bpy.props.BoolProperty(default=False)

def register_properties():
    bpy.utils.register_class(VideomockupCurveSettings)
    bpy.types.Scene.videomockup_curves = bpy.props.PointerProperty(type=VideomockupCurveSettings)
    
    bpy.types.Scene.videomockup_highlight_curve_toggle = bpy.props.BoolProperty(
        name="Highlight Curve Mode",
        default=True,
        update=update_highlight_curve
    )

    bpy.types.Scene.videomockup_shadow_curve_toggle = bpy.props.BoolProperty(
        name="Shadow Curve Mode",
        default=True,
        update=update_shadow_curve
    )
    
    bpy.types.Scene.videomockup_selected_output = bpy.props.StringProperty(
        name="Selected Output",
        default="Image Render"
    )
    
def unregister_properties():
    del bpy.types.Scene.videomockup_selected_output
    del bpy.types.Scene.videomockup_shadow_curve_toggle
    del bpy.types.Scene.videomockup_highlight_curve_toggle
    del bpy.types.Scene.videomockup_curves
    bpy.utils.unregister_class(VideomockupCurveSettings)

class VIDEOMOCKUP_OT_add_nodes(Operator):
    """Add the video mockup output node setup"""
    bl_idname = "videomockup.add_nodes"
    bl_label = "Add Nodes"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Always enable nodes, never turn them off
        context.scene.use_nodes = True
        
        # Enable required render passes in all view layers
        for view_layer in context.scene.view_layers:
            try:
                # MANUALLY ENABLE ALL REQUIRED PASSES
                
                # First try direct attribute access based on screenshot
                if hasattr(view_layer, 'cryptomatte'):
                    # For Blender 4.x structure
                    self.report({'INFO'}, "Using direct cryptomatte attribute")
                    view_layer.cryptomatte.object = True
                    view_layer.cryptomatte.material = True
                    
                # If direct access failed, try the traditional approach
                elif hasattr(view_layer, 'use_pass_crypto_object'):
                    self.report({'INFO'}, "Using use_pass_crypto_object attribute")
                    view_layer.use_pass_crypto_object = True
                    view_layer.use_pass_crypto_material = True
                    
                # If that failed too, try accessing through cycles
                elif hasattr(view_layer, 'cycles') and hasattr(view_layer.cycles, 'use_pass_crypto_object'):
                    self.report({'INFO'}, "Using cycles.use_pass_crypto_object attribute")
                    view_layer.cycles.use_pass_crypto_object = True
                    view_layer.cycles.use_pass_crypto_material = True
                    
                # Last resort - find the checkbox directly using Python's dir function
                else:
                    for attr_name in dir(view_layer):
                        if 'crypto' in attr_name.lower() and 'object' in attr_name.lower():
                            self.report({'INFO'}, f"Found crypto attribute: {attr_name}")
                            setattr(view_layer, attr_name, True)
                        if 'crypto' in attr_name.lower() and 'material' in attr_name.lower():
                            self.report({'INFO'}, f"Found crypto attribute: {attr_name}")
                            setattr(view_layer, attr_name, True)
                
                # Enable Diffuse Color
                if hasattr(view_layer, 'light') and hasattr(view_layer.light, 'diffuse') and hasattr(view_layer.light.diffuse, 'color'):
                    self.report({'INFO'}, "Using light.diffuse.color attribute")
                    view_layer.light.diffuse.color = True
                elif hasattr(view_layer, 'lightgroups') and hasattr(view_layer.lightgroups, 'diffuse') and hasattr(view_layer.lightgroups.diffuse, 'color'):
                    self.report({'INFO'}, "Using lightgroups.diffuse.color attribute")
                    view_layer.lightgroups.diffuse.color = True
                elif hasattr(view_layer, 'use_pass_diffuse_color'):
                    self.report({'INFO'}, "Using use_pass_diffuse_color attribute")
                    view_layer.use_pass_diffuse_color = True
                elif hasattr(view_layer, 'cycles') and hasattr(view_layer.cycles, 'use_pass_diffuse_color'):
                    self.report({'INFO'}, "Using cycles.use_pass_diffuse_color attribute")
                    view_layer.cycles.use_pass_diffuse_color = True
                
            except Exception as e:
                self.report({'WARNING'}, f"Could not enable all passes for view layer {view_layer.name}: {str(e)}")
                
        # Specifically try to find and touch the UI elements directly - last resort hack
        try:
            for screen in bpy.data.screens:
                for area in screen.areas:
                    if area.type == 'PROPERTIES':
                        for space in area.spaces:
                            if space.type == 'PROPERTIES':
                                # Trigger a UI update that might refresh the passes
                                space.context = 'VIEW_LAYER'
                                # Force a redraw
                                area.tag_redraw()
                                # Try to access data directly
                                context.window_manager.update_tag()
        except:
            pass
        
        # Clear existing nodes in the compositor
        node_tree = context.scene.node_tree
        for node in node_tree.nodes:
            node_tree.nodes.remove(node)
        
        # Clean up any existing node groups
        for group_name in ["Highlight/Shadow Preview", "RGB", "All Previews"]:
            if group_name in bpy.data.node_groups:
                bpy.data.node_groups.remove(bpy.data.node_groups[group_name])
        
        # Create the node groups in the correct order
        self.create_highlight_shadow_preview_group()
        self.create_rgb_group()
        self.create_all_previews_group(context)
        
        # Create the main compositor nodes
        # Render Layers
        render_layer = node_tree.nodes.new('CompositorNodeRLayers')
        render_layer.name = "Render Layers.001"
        render_layer.location = (-660.0900268554688, 263.6499938964844)
        render_layer.width = 240.0

        # Cryptomatte
        cryptomatte = node_tree.nodes.new('CompositorNodeCryptomatteV2')
        cryptomatte.name = "Cryptomatte.001"
        cryptomatte.location = (-335.45751953125, 174.3000030517578)
        cryptomatte.width = 240.0

        # All Previews group node
        all_previews = node_tree.nodes.new('CompositorNodeGroup')
        all_previews.node_tree = bpy.data.node_groups["All Previews"]
        all_previews.name = "All Previews"
        all_previews.label = "All Previews"
        all_previews.location = (-35.51734161376953, 296.1400146484375)
        all_previews.width = 140.0

        # Add a direct composite node to ensure viewing is not affected
        composite = node_tree.nodes.new('CompositorNodeComposite')
        composite.name = "Main Composite"
        composite.location = (-300.9775390625, 400.0)

        # Add a single viewer node outside the groups
        viewer = node_tree.nodes.new('CompositorNodeViewer')
        viewer.name = "Main Viewer"
        viewer.location = (158.8904266357422, 296.1400146484375)
        
        # Connect the render layer directly to the composite to maintain normal render
        node_tree.links.new(render_layer.outputs['Image'], composite.inputs['Image'])
        
        # Create the main connections
        node_tree.links.new(render_layer.outputs['Image'], cryptomatte.inputs['Image'])
        node_tree.links.new(render_layer.outputs['Image'], all_previews.inputs['Image'])
        
        # Connect all_previews output to the main viewer
        node_tree.links.new(all_previews.outputs['Viewer'], viewer.inputs['Image'])
        
        # Check what diffuse color output is available and connect it
        diff_col_output = None
        for output in render_layer.outputs:
            if output.name in ['DiffCol', 'Diffuse Color', 'DiffDir', 'Diffuse Direct']:
                diff_col_output = output.name
                break
                
        if diff_col_output:
            node_tree.links.new(render_layer.outputs[diff_col_output], all_previews.inputs['DiffCol'])
        else:
            # If no diffuse color output is found, use Image as fallback
            node_tree.links.new(render_layer.outputs['Image'], all_previews.inputs['DiffCol'])
            self.report({'WARNING'}, "Diffuse Color output not found, using Image instead")
            
        node_tree.links.new(cryptomatte.outputs['Matte'], all_previews.inputs['Matte'])
        
        # Force the "Image Render" to be selected by default
        context.scene.videomockup_selected_output = "Image Render"
        # Call the switch output operator to ensure proper connection
        bpy.ops.videomockup.switch_output(output_name="Image Render")
        
        # Define a function to refresh viewers and backdrops
        def refresh_viewers_and_backdrops():
            # Refresh viewer node by toggling mute
            viewer = node_tree.nodes.get("Main Viewer")
            if viewer:
                viewer.mute = True
                viewer.mute = False
            
            # Refresh backdrop in all NODE_EDITOR areas
            for area in context.screen.areas:
                if area.type == 'NODE_EDITOR':
                    for space in area.spaces:
                        if space.type == 'NODE_EDITOR':
                            # Force backdrop on
                            space.show_backdrop = False
                            space.show_backdrop = True
        
        # Do the refresh
        refresh_viewers_and_backdrops()
        
        self.report({'INFO'}, "Videomockup output nodes added successfully")
        return {'FINISHED'}
        
    def create_highlight_shadow_preview_group(self):
        group = bpy.data.node_groups.new(name="Highlight/Shadow Preview", type="CompositorNodeTree")

        # Group I/O
        group_input = group.nodes.new('NodeGroupInput')
        group_input.location = (-761.631, 130.0)

        group_output = group.nodes.new('NodeGroupOutput')
        group_output.location = (470.917, 136.976)

        # Interface Sockets
        group.interface.new_socket(name="Image", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Matte", in_out='INPUT', socket_type='NodeSocketFloat')
        group.interface.new_socket(name="DiffCol", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Image Highlight", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Image Shadow", in_out='INPUT', socket_type='NodeSocketColor')

        group.interface.new_socket(name="Image Render", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Background", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Crop", in_out='OUTPUT', socket_type='NodeSocketFloat')
        group.interface.new_socket(name="Highlight", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Highlight Preview", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Shadow", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Shadow Preview", in_out='OUTPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Highlight + Shadow Preview", in_out='OUTPUT', socket_type='NodeSocketColor')

        # Create the nodes exactly matching JSON
        alpha_over_ph = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_ph.name = "Alpha Over PH"
        alpha_over_ph.location = (-356.836, 277.892)
        alpha_over_ph.use_premultiply = False

        alpha_over_bg = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_bg.name = "Alpha Over BG"
        alpha_over_bg.location = (-356.836, 456.457)
        alpha_over_bg.use_premultiply = False

        image_node = group.nodes.new('CompositorNodeImage')
        image_node.name = "Image"
        image_node.label = "Image"
        image_node.location = (-761.391, -71.677)

        transform_node = group.nodes.new('CompositorNodeTransform')
        transform_node.name = "Transform"
        transform_node.label = "Transform"
        transform_node.location = (-574.086, -71.677)

        # Initialize default values for Set Alpha nodes
        set_alpha = group.nodes.new('CompositorNodeSetAlpha')
        set_alpha.name = "Set Alpha"
        set_alpha.location = (-133.116, 235.496)
        set_alpha.inputs[0].default_value = (1.0, 1.0, 1.0, 1.0)  # Default white color

        invert_color = group.nodes.new('CompositorNodeInvert')
        invert_color.name = "Invert Color"
        invert_color.location = (-356.836, -17.917)

        set_alpha_001 = group.nodes.new('CompositorNodeSetAlpha')
        set_alpha_001.name = "Set Alpha.001"
        set_alpha_001.location = (-133.116, -17.917)
        set_alpha_001.inputs[0].default_value = (0.0, 0.0, 0.0, 1.0)  # Default black color

        alpha_over_hlsh = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_hlsh.name = "Alpha Over HL+SH"
        alpha_over_hlsh.location = (85.654, -208.726)
        alpha_over_hlsh.use_premultiply = False

        alpha_over_hlsh_2 = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_hlsh_2.name = "Alpha Over HL+SH 2"
        alpha_over_hlsh_2.location = (282.156, -58.496)
        alpha_over_hlsh_2.use_premultiply = False

        alpha_over_hl = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_hl.name = "Alpha Over HL"
        alpha_over_hl.location = (85.654, 277.892)
        alpha_over_hl.use_premultiply = False

        alpha_over_sh = group.nodes.new('CompositorNodeAlphaOver')
        alpha_over_sh.name = "Alpha Over SH"
        alpha_over_sh.location = (85.654, -17.917)
        alpha_over_sh.use_premultiply = False

        rgb_node = group.nodes.new('CompositorNodeRGB')
        rgb_node.name = "RGB"
        rgb_node.location = (-574.086, -286.292)

        # Create connections matching the JSON file's link data
        # Basic connections
        group.links.new(image_node.outputs["Image"], transform_node.inputs["Image"])
        
        # Alpha Over PH connections
        group.links.new(group_input.outputs["Matte"], alpha_over_ph.inputs["Fac"])
        group.links.new(group_input.outputs["Image"], alpha_over_ph.inputs[1])  # Connect Image to background image
        group.links.new(group_input.outputs["DiffCol"], alpha_over_ph.inputs[2])  # Connect DiffCol to foreground image
        
        # Alpha Over BG connections
        group.links.new(group_input.outputs["Matte"], alpha_over_bg.inputs["Fac"])
        group.links.new(group_input.outputs["Image"], alpha_over_bg.inputs[1])  # Connect Image to background image
        group.links.new(group_input.outputs["DiffCol"], alpha_over_bg.inputs[2])  # Connect DiffCol to foreground image
        
        # Group input connections to output
        group.links.new(group_input.outputs["Image"], group_output.inputs["Image Render"])
        group.links.new(group_input.outputs["Matte"], group_output.inputs["Crop"])
        group.links.new(group_input.outputs["Image Highlight"], group_output.inputs["Highlight"])
        group.links.new(group_input.outputs["Image Shadow"], group_output.inputs["Shadow"])
        
        # Alpha inputs
        group.links.new(group_input.outputs["Image Highlight"], set_alpha.inputs["Alpha"])
        group.links.new(group_input.outputs["Image Shadow"], invert_color.inputs["Color"])
        group.links.new(invert_color.outputs["Color"], set_alpha_001.inputs["Alpha"])
        
        # Output from Alpha Over BG to Background output
        group.links.new(alpha_over_bg.outputs["Image"], group_output.inputs["Background"])
        
        # Connection to Alpha Over HL+SH from Set Alpha nodes
        group.links.new(set_alpha.outputs["Image"], alpha_over_hlsh.inputs[1])  # Set Alpha → foreground image
        group.links.new(set_alpha_001.outputs["Image"], alpha_over_hlsh.inputs[2])  # Set Alpha.001 → background image
        
        # Connect Alpha Over HL+SH to Alpha Over HL+SH 2
        group.links.new(alpha_over_hlsh.outputs["Image"], alpha_over_hlsh_2.inputs[2])  # Alpha Over HL+SH → foreground image
        group.links.new(alpha_over_ph.outputs["Image"], alpha_over_hlsh_2.inputs[1])  # Alpha Over PH → background image (UPDATED)
        
        # Connect Alpha Over HL+SH 2 to Group Output
        group.links.new(alpha_over_hlsh_2.outputs["Image"], group_output.inputs["Highlight + Shadow Preview"])
        
        # Alpha Over HL connections
        group.links.new(alpha_over_ph.outputs["Image"], alpha_over_hl.inputs[1])  # Alpha Over PH → background image (UPDATED)
        group.links.new(set_alpha.outputs["Image"], alpha_over_hl.inputs[2])  # Set Alpha → foreground image
        group.links.new(alpha_over_hl.outputs["Image"], group_output.inputs["Highlight Preview"])
        
        # Alpha Over SH connections
        group.links.new(alpha_over_ph.outputs["Image"], alpha_over_sh.inputs[1])  # Alpha Over PH → background image (UPDATED)
        group.links.new(set_alpha_001.outputs["Image"], alpha_over_sh.inputs[2])  # Set Alpha.001 → foreground image
        group.links.new(alpha_over_sh.outputs["Image"], group_output.inputs["Shadow Preview"])

        return group

    def create_rgb_group(self):
        # Create a new node group
        group = bpy.data.node_groups.new(name="RGB", type="CompositorNodeTree")
        
        # Create group input/output nodes
        group_input = group.nodes.new('NodeGroupInput')
        group_input.location = (-388.46, 0.0)
        
        group_output = group.nodes.new('NodeGroupOutput')
        group_output.location = (271.09, -6.46)
        
        # Add input sockets using interface
        group.interface.new_socket(name="Crop", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Highlight", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Shadow", in_out='INPUT', socket_type='NodeSocketColor')
        
        # Add output sockets using interface
        group.interface.new_socket(name="Image", in_out='OUTPUT', socket_type='NodeSocketColor')
        
        # Create the internal nodes - using CompositorNodeSeparateColor
        separate_color = group.nodes.new('CompositorNodeSeparateColor')
        separate_color.name = "Separate Color"
        separate_color.location = (-188.46, 185.63)
        separate_color.use_custom_color = True
        separate_color.color = (0.16, 0.16, 0.80)  # Blue color
        
        separate_color1 = group.nodes.new('CompositorNodeSeparateColor')
        separate_color1.name = "Separate Color.001"
        separate_color1.location = (-188.46, 0.0)
        separate_color1.use_custom_color = True
        separate_color1.color = (0.80, 0.16, 0.16)  # Red color
        
        separate_color2 = group.nodes.new('CompositorNodeSeparateColor')
        separate_color2.name = "Separate Color.002"
        separate_color2.location = (-188.46, -185.63)
        separate_color2.use_custom_color = True
        separate_color2.color = (0.16, 0.80, 0.16)  # Green color
        
        combine_color = group.nodes.new('CompositorNodeCombineColor')
        combine_color.name = "Combine Color"
        combine_color.location = (81.09, 2.35)
        
        # Create the connections
        # Separate Colors to Combine Color
        group.links.new(separate_color2.outputs['Green'], combine_color.inputs['Green'])
        group.links.new(separate_color.outputs['Blue'], combine_color.inputs['Blue'])
        group.links.new(separate_color1.outputs['Red'], combine_color.inputs['Red'])
        
        # Group Input to Separate Colors
        group.links.new(group_input.outputs['Highlight'], separate_color1.inputs[0])
        group.links.new(group_input.outputs['Crop'], separate_color.inputs[0])
        group.links.new(group_input.outputs['Shadow'], separate_color2.inputs[0])
        
        # Combine Color to Group Output
        group.links.new(combine_color.outputs[0], group_output.inputs['Image'])
        
        return group

    def create_all_previews_group(self, context):
        group = bpy.data.node_groups.new(name="All Previews", type="CompositorNodeTree")

        # Group Inputs/Outputs
        group_input = group.nodes.new("NodeGroupInput")
        group_input.location = (-1160.542, -40.007)  # Exact coordinates from JSON
        group.interface.new_socket(name="Image", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="DiffCol", in_out='INPUT', socket_type='NodeSocketColor')
        group.interface.new_socket(name="Matte", in_out='INPUT', socket_type='NodeSocketFloat')

        group_output = group.nodes.new("NodeGroupOutput")
        group_output.location = (257.935, -9.554)  # Exact coordinates from JSON
        group.interface.new_socket(name="Viewer", in_out='OUTPUT', socket_type='NodeSocketColor')

        # RGB to BW node
        rgb_to_bw = group.nodes.new("CompositorNodeRGBToBW")
        rgb_to_bw.name = "RGB to BW"
        rgb_to_bw.location = (-931.338, -58.090)

        # Highlight/Shadow Preview group node
        hs_preview = group.nodes.new("CompositorNodeGroup")
        hs_preview.name = "Highlight/Shadow Preview"
        hs_preview.label = "Highlight/Shadow Preview"
        hs_preview.node_tree = bpy.data.node_groups["Highlight/Shadow Preview"]
        hs_preview.location = (-203.361, 200.0)

        # RGB group node
        rgb_node = group.nodes.new("CompositorNodeGroup")
        rgb_node.name = "RGB"
        rgb_node.label = "RGB"
        rgb_node.node_tree = bpy.data.node_groups["RGB"]
        rgb_node.location = (22.568, 120.0)

        # RGB Curves nodes
        curves_highlight = group.nodes.new("CompositorNodeCurveRGB")
        curves_highlight.name = "RGB Curves Highlight"
        curves_highlight.label = "RGB Curves Highlight"
        curves_highlight.location = (-703.361, 300.0)
        curves_highlight.width = 200.0

        curves_shadow = group.nodes.new("CompositorNodeCurveRGB")
        curves_shadow.name = "RGB Curves Shadow"
        curves_shadow.label = "RGB Curves Shadow"
        curves_shadow.location = (-703.361, -100.0)
        curves_shadow.width = 200.0

        # Mix nodes
        mix_highlight = group.nodes.new("CompositorNodeMixRGB")
        mix_highlight.name = "Mix"
        mix_highlight.location = (-423.361, 300.0)
        mix_highlight.blend_type = "MIX"
        mix_highlight.use_clamp = False
        mix_highlight.inputs[1].default_value = (0.0, 0.0, 0.0, 1.0)  # Default color for Mix

        mix_shadow = group.nodes.new("CompositorNodeMixRGB")
        mix_shadow.name = "Mix.001"
        mix_shadow.location = (-423.361, -100.0)
        mix_shadow.blend_type = "MIX"
        mix_shadow.use_clamp = False
        mix_shadow.inputs[1].default_value = (1.0, 1.0, 1.0, 1.0)  # Default color for Mix

        # File Output nodes
        out_image = group.nodes.new("CompositorNodeOutputFile")
        out_image.name = "File Output Image Render"
        out_image.label = "File Output Image Render"
        out_image.location = (255.516, 400.0)
        out_image.mute = True
        out_image.use_custom_color = True
        out_image.color = (0, 0.7, 0)  # Green color

        out_bg = group.nodes.new("CompositorNodeOutputFile")
        out_bg.name = "File Output Background"
        out_bg.label = "File Output Background"
        out_bg.location = (255.516, 270.0)
        out_bg.mute = True
        out_bg.use_custom_color = True
        out_bg.color = (0, 0.7, 0)  # Green color

        out_rgb = group.nodes.new("CompositorNodeOutputFile")
        out_rgb.name = "File Output RGB"
        out_rgb.label = "File Output RGB"
        out_rgb.location = (255.516, 150.0)
        out_rgb.mute = True
        out_rgb.use_custom_color = True
        out_rgb.color = (0, 0.7, 0)  # Green color
        out_rgb.format.color_management = 'OVERRIDE'
        out_rgb.format.display_settings.display_device = 'sRGB'
        out_rgb.format.view_settings.view_transform = 'Standard'
        out_rgb.format.view_settings.look = 'None'

        # Create connections according to JSON link data
        # RGB to BW from Image input
        group.links.new(group_input.outputs["Image"], rgb_to_bw.inputs["Image"])
        
        # RGB Curves from RGB to BW
        group.links.new(rgb_to_bw.outputs["Val"], curves_highlight.inputs["Image"])
        group.links.new(rgb_to_bw.outputs["Val"], curves_shadow.inputs["Image"])
        
        # Mix nodes
        group.links.new(curves_highlight.outputs["Image"], mix_highlight.inputs[2])
        group.links.new(group_input.outputs["Matte"], mix_highlight.inputs[0])
        group.links.new(curves_shadow.outputs["Image"], mix_shadow.inputs[2])
        group.links.new(group_input.outputs["Matte"], mix_shadow.inputs[0])
        
        # Links to Highlight/Shadow Preview
        group.links.new(mix_highlight.outputs["Image"], hs_preview.inputs["Image Highlight"])
        group.links.new(mix_shadow.outputs["Image"], hs_preview.inputs["Image Shadow"])
        group.links.new(group_input.outputs["Image"], hs_preview.inputs["Image"])
        group.links.new(group_input.outputs["DiffCol"], hs_preview.inputs["DiffCol"])
        group.links.new(group_input.outputs["Matte"], hs_preview.inputs["Matte"])
        
        # Links to RGB node
        group.links.new(hs_preview.outputs["Highlight"], rgb_node.inputs["Highlight"])
        group.links.new(hs_preview.outputs["Crop"], rgb_node.inputs["Crop"])
        group.links.new(hs_preview.outputs["Shadow"], rgb_node.inputs["Shadow"])
        
        # Output file connections
        group.links.new(hs_preview.outputs["Image Render"], out_image.inputs["Image"])
        group.links.new(hs_preview.outputs["Background"], out_bg.inputs["Image"])
        group.links.new(rgb_node.outputs["Image"], out_rgb.inputs["Image"])
        
        # Important: Connect "Image Render" output to Viewer (matches the exported JSON)
        group.links.new(hs_preview.outputs["Image Render"], group_output.inputs["Viewer"])

        return group

class VIDEOMOCKUP_OT_cryptomatte_object(Operator):
    """Set Cryptomatte to Object mode"""
    bl_idname = "videomockup.cryptomatte_object"
    bl_label = "Object"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Find the Cryptomatte node
        node_tree = context.scene.node_tree
        if not node_tree:
            self.report({'ERROR'}, "No active compositor node tree")
            return {'CANCELLED'}
            
        for node in node_tree.nodes:
            if node.bl_idname == 'CompositorNodeCryptomatteV2':
                try:
                    # First, preserve any existing matte_id
                    matte_id = ""
                    if hasattr(node, "matte_id"):
                        matte_id = node.matte_id
                    
                    # In Blender 4.3.2, the dropdown is controlled by this
                    node.layer_name = "ViewLayer.CryptoObject"
                    
                    # Restore the matte_id
                    if hasattr(node, "matte_id") and matte_id:
                        node.matte_id = matte_id
                    
                    return {'FINISHED'}
                except Exception as e:
                    self.report({'ERROR'}, f"Could not set Cryptomatte to Object mode: {str(e)}")
                    
                    # Fallback: try to find the right property by looking at all string properties
                    try:
                        for prop_name in dir(node):
                            if prop_name.startswith('__'):
                                continue
                            
                            try:
                                # See if this property contains a string value with ViewLayer
                                prop_value = getattr(node, prop_name)
                                if isinstance(prop_value, str) and "ViewLayer" in prop_value:
                                    # Try changing to CryptoObject
                                    if "CryptoMaterial" in prop_value:
                                        new_value = prop_value.replace("CryptoMaterial", "CryptoObject")
                                        setattr(node, prop_name, new_value)
                                        return {'FINISHED'}
                            except:
                                pass
                    except:
                        pass
                        
                    return {'CANCELLED'}
        
        self.report({'ERROR'}, "No Cryptomatte node found")
        return {'CANCELLED'}

class VIDEOMOCKUP_OT_cryptomatte_material(Operator):
    """Set Cryptomatte to Material mode"""
    bl_idname = "videomockup.cryptomatte_material"
    bl_label = "Material"
    bl_options = {'REGISTER', 'UNDO'}
    
    def execute(self, context):
        # Find the Cryptomatte node
        node_tree = context.scene.node_tree
        if not node_tree:
            self.report({'ERROR'}, "No active compositor node tree")
            return {'CANCELLED'}
            
        for node in node_tree.nodes:
            if node.bl_idname == 'CompositorNodeCryptomatteV2':
                try:
                    # First, preserve any existing matte_id
                    matte_id = ""
                    if hasattr(node, "matte_id"):
                        matte_id = node.matte_id
                    
                    # In Blender 4.3.2, the dropdown is controlled by this
                    node.layer_name = "ViewLayer.CryptoMaterial"
                    
                    # Restore the matte_id
                    if hasattr(node, "matte_id") and matte_id:
                        node.matte_id = matte_id
                    
                    return {'FINISHED'}
                except Exception as e:
                    self.report({'ERROR'}, f"Could not set Cryptomatte to Material mode: {str(e)}")
                    
                    # Fallback: try to find the right property by looking at all string properties
                    try:
                        for prop_name in dir(node):
                            if prop_name.startswith('__'):
                                continue
                            
                            try:
                                # See if this property contains a string value with ViewLayer
                                prop_value = getattr(node, prop_name)
                                if isinstance(prop_value, str) and "ViewLayer" in prop_value:
                                    # Try changing to CryptoMaterial
                                    if "CryptoObject" in prop_value:
                                        new_value = prop_value.replace("CryptoObject", "CryptoMaterial")
                                        setattr(node, prop_name, new_value)
                                        return {'FINISHED'}
                            except:
                                pass
                    except:
                        pass
                        
                    return {'CANCELLED'}
        
        self.report({'ERROR'}, "No Cryptomatte node found")
        return {'CANCELLED'}

class VIDEOMOCKUP_PT_node_editor(Panel):
    bl_label = "Videomockup Outputs"
    bl_idname = "VIDEOMOCKUP_PT_node_editor"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Videomockup'

    @classmethod
    def poll(cls, context):
        return context.space_data.tree_type == 'CompositorNodeTree'

    def draw(self, context):
        draw_videomockup_ui(self, context)
class VIDEOMOCKUP_PT_sequencer(Panel):
    bl_label = "Videomockup Outputs"
    bl_idname = "VIDEOMOCKUP_PT_sequencer"
    bl_space_type = 'SEQUENCE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Videomockup'

    @classmethod
    def poll(cls, context):
        # Show panel in all Sequencer display modes (strip, preview, combined)
        return context.space_data.view_type in {
            'SEQUENCER', 'PREVIEW', 'SEQUENCER_PREVIEW'
        }

    def draw(self, context):
        draw_videomockup_ui(self, context)

class VIDEOMOCKUP_OT_save_viewer_image(bpy.types.Operator, ExportHelper):
    bl_idname = "videomockup.save_viewer_image"
    bl_label = "Save Viewer Image"
    bl_description = "Save the Viewer Node image using Blender's full format list"
    filename_ext = ".png"

    filter_glob: StringProperty(
        default="*.bmp;*.cin;*.dpx;*.exr;*.hdr;*.iris;*.jpg;*.jpeg;*.jp2;*.png;*.tga;*.tif;*.tiff",
        options={'HIDDEN'},
    )

    file_format: EnumProperty(
        name="Format",
        items=[
            ('BMP', "BMP", ""),
            ('IRIS', "IRIS", ""),
            ('PNG', "PNG", ""),
            ('JPEG', "JPEG", ""),
            ('JPEG2000', "JPEG2000", ""),
            ('TARGA', "TARGA", ""),
            ('CINEON', "CINEON", ""),
            ('DPX', "DPX", ""),
            ('OPEN_EXR', "OPEN_EXR", ""),
            ('HDR', "HDR", ""),
            ('TIFF', "TIFF", ""),
        ],
        default='PNG',
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "file_format")

    def invoke(self, context, event):
        # Set default extension based on file format before opening dialog
        ext_map = {
            'BMP': '.bmp',
            'IRIS': '.iris',
            'PNG': '.png',
            'JPEG': '.jpg',
            'JPEG2000': '.jp2',
            'TARGA': '.tga',
            'CINEON': '.cin',
            'DPX': '.dpx',
            'OPEN_EXR': '.exr',
            'HDR': '.hdr',
            'TIFF': '.tif',
        }
        self.filename_ext = ext_map.get(self.file_format, '.png')
        return ExportHelper.invoke(self, context, event)

    def check(self, context):
        # Automatically update the filename extension when format changes
        ext_map = {
            'BMP': '.bmp',
            'IRIS': '.iris',
            'PNG': '.png',
            'JPEG': '.jpg',
            'JPEG2000': '.jp2',
            'TARGA': '.tga',
            'CINEON': '.cin',
            'DPX': '.dpx',
            'OPEN_EXR': '.exr',
            'HDR': '.hdr',
            'TIFF': '.tif',
        }
        ext = ext_map.get(self.file_format, '.png')
        base, _ = os.path.splitext(self.filepath)
        new_path = base + ext
        if self.filepath != new_path:
            self.filepath = new_path
            return True
        return False

    def execute(self, context):
        image = bpy.data.images.get("Viewer Node")
        if not image:
            self.report({'ERROR'}, "Viewer Node image not found")
            return {'CANCELLED'}

        # Ensure correct extension
        ext_map = {
            'BMP': '.bmp',
            'IRIS': '.iris',
            'PNG': '.png',
            'JPEG': '.jpg',
            'JPEG2000': '.jp2',
            'TARGA': '.tga',
            'CINEON': '.cin',
            'DPX': '.dpx',
            'OPEN_EXR': '.exr',
            'HDR': '.hdr',
            'TIFF': '.tif',
        }
        ext = ext_map.get(self.file_format, '.png')
        base, _ = os.path.splitext(self.filepath)
        final_path = base + ext

        try:
            image.file_format = self.file_format
            image.save_render(final_path)
            self.report({'INFO'}, f"Saved to: {final_path}")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Failed to save: {e}")
            return {'CANCELLED'}
        
class VIDEOMOCKUP_OT_switch_output(bpy.types.Operator):
    bl_idname = "videomockup.switch_output"
    bl_label = "Switch Group Output"
    bl_description = "Switch output from nested node groups to the main group output"

    output_name: bpy.props.StringProperty()

    def execute(self, context):
        # Get All Previews group
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            self.report({'ERROR'}, "All Previews group not found")
            return {'CANCELLED'}

        # Get nodes in All Previews
        group_output = next((n for n in all_preview_group.nodes if n.bl_idname == "NodeGroupOutput"), None)
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        rgb_node = all_preview_group.nodes.get("RGB")

        if not group_output or not hs_node or not rgb_node:
            self.report({'ERROR'}, "Required nodes not found in All Previews group")
            return {'CANCELLED'}

        # Get viewer socket
        viewer_socket = group_output.inputs.get("Viewer")
        if not viewer_socket:
            self.report({'ERROR'}, "'Viewer' socket not found in Group Output")
            return {'CANCELLED'}

        # Remove existing connections to viewer_socket
        for link in list(all_preview_group.links):
            if link.to_node == group_output and link.to_socket == viewer_socket:
                all_preview_group.links.remove(link)

        # Set up new connections based on selected output
        if self.output_name == "RGB":
            # For RGB output, connect directly in All Previews group
            source_socket = rgb_node.outputs.get("Image")
            if not source_socket:
                self.report({'ERROR'}, "'Image' output not found in RGB node")
                return {'CANCELLED'}
            all_preview_group.links.new(source_socket, viewer_socket)
        else:
            # For other outputs, connect from HS Preview node
            source_socket = hs_node.outputs.get(self.output_name)
            if not source_socket:
                self.report({'ERROR'}, f"Output '{self.output_name}' not found in Highlight/Shadow Preview node")
                return {'CANCELLED'}
            all_preview_group.links.new(source_socket, viewer_socket)

        # Store current output selection
        context.scene.videomockup_selected_output = self.output_name
        
        return {'FINISHED'}

class VIDEOMOCKUP_PT_image_editor(Panel):
    bl_label = "Videomockup Outputs"
    bl_idname = "VIDEOMOCKUP_PT_image_editor"
    bl_space_type = 'IMAGE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'Videomockup'

    @classmethod
    def poll(cls, context):
        return context.space_data.type == 'IMAGE_EDITOR'

    def draw(self, context):
        draw_videomockup_ui(self, context)

def draw_videomockup_ui(self, context):
    layout = self.layout
    props = context.scene.videomockup_curves
    
    # Video Output Layers header and content
    layout.prop(props, "show_outputs", text="Video Output Layers", emboss=True, icon='TRIA_DOWN' if props.show_outputs else 'TRIA_RIGHT')
    
    if props.show_outputs:
        # Check if the compositor setup exists
        has_compositor_setup = False
        if context.scene.use_nodes and context.scene.node_tree:
            # Check for essential nodes in the compositor
            for node in context.scene.node_tree.nodes:
                if node.name == "All Previews" and node.bl_idname == 'CompositorNodeGroup':
                    if node.node_tree and node.node_tree.name == "All Previews":
                        has_compositor_setup = True
                        break
        
        # Always show the Add Nodes button
        box_add = layout.box()
        add_button = box_add.row()
        add_button.scale_y = 1.5  # Makes the button 50% taller
        add_button.operator("videomockup.add_nodes", text="Add Nodes", icon='NODETREE')
        
        # Only show the rest of the UI if nodes have been added
        if has_compositor_setup:
            active_mode = "NONE"
            crypto_node = None
            
            box = layout.box()
            box.label(text="Crop Mask", icon='IMAGE_ZDEPTH')

            node_tree = context.scene.node_tree
            if node_tree:
                for node in node_tree.nodes:
                    if node.bl_idname == 'CompositorNodeCryptomatteV2':
                        crypto_node = node
                        for prop_name in dir(node):
                            if prop_name.startswith('__'):
                                continue
                            try:
                                val = getattr(node, prop_name)
                                if isinstance(val, str) and 'Crypto' in val:
                                    if 'Object' in val:
                                        active_mode = "OBJECT"
                                    elif 'Material' in val:
                                        active_mode = "MATERIAL"
                            except:
                                pass
                        break

            row = box.row(align=True)
            row.operator("videomockup.cryptomatte_object", text="Object", depress=(active_mode == "OBJECT"))
            row.operator("videomockup.cryptomatte_material", text="Material", depress=(active_mode == "MATERIAL"))

            if crypto_node:
                matte_id = getattr(crypto_node, 'matte_id', '')

                # Picker icon + editable field
                row = box.row(align=True)
                row.label(icon='EYEDROPPER')
                row.prop(crypto_node, "matte_id", text="")

                # Warning if empty
                if not matte_id.strip():
                    warning = box.row()
                    warning.alert = True
                    warning.label(text="No Matte ID selected", icon='ERROR')

            # Instructional info (always shown)
            box.label(text="Pick Matte ID in Cryptomatte node", icon='INFO')
            
            # Viewer Node Selector Box
            viewer_box = layout.box()
            header = viewer_box.row(align=True)
            header.label(text="Select Viewer Output", icon='RESTRICT_SELECT_OFF')

            save_btn = header.row()
            save_btn.alignment = 'RIGHT'
            save_btn.scale_x = 1.2
            save_btn.scale_y = 1.2
            save_btn.operator("videomockup.save_viewer_image", text="", icon='FILE_TICK')

            group = bpy.data.node_groups.get("All Previews")
            if group:
                hs_node = group.nodes.get("Highlight/Shadow Preview")
                rgb_node = group.nodes.get("RGB")
                group_output = next((n for n in group.nodes if n.bl_idname == "NodeGroupOutput"), None)
                outputs = [
                    "Image Render", "Background", "Crop",
                    "Highlight", "Highlight Preview",
                    "Shadow", "Shadow Preview", "Highlight + Shadow Preview", "RGB"
                ]

                # Get the currently selected output from the property
                current_output = context.scene.videomockup_selected_output
                
                # Draw a button for each output
                for out_name in outputs:
                    is_active = (out_name == current_output)
                    
                    row = viewer_box.row()
                    row.operator(
                        "videomockup.switch_output",
                        text=out_name,
                        depress=is_active
                    ).output_name = out_name

            # RGB Curves section
            if group:
                box_curve = layout.box()
                box_curve.label(text="RGB Curves", icon='CURVE_DATA')

                rgb_high = group.nodes.get("RGB Curves Highlight")
                rgb_shadow = group.nodes.get("RGB Curves Shadow")

                if rgb_high and rgb_shadow:
                    row = box_curve.row(align=True)
                    row.label(text="Highlight Curve")
                    
                    toggle_text = "ON" if context.scene.videomockup_curves.highlight_curve_toggle else "OFF"
                    
                    op = row.operator(
                        "videomockup.toggle_highlight_curve", 
                        text=toggle_text, 
                        depress=context.scene.videomockup_curves.highlight_curve_toggle
                    )
                    
                    curve_box = box_curve.column()
                    curve_box.enabled = context.scene.videomockup_curves.highlight_curve_toggle
                    curve_box.template_curve_mapping(rgb_high, "mapping", type='COLOR')

                    row = box_curve.row(align=True)
                    row.label(text="Shadow Curve")
                    
                    toggle_text = "ON" if context.scene.videomockup_curves.shadow_curve_toggle else "OFF"
                    
                    op = row.operator(
                        "videomockup.toggle_shadow_curve", 
                        text=toggle_text, 
                        depress=context.scene.videomockup_curves.shadow_curve_toggle
                    )
                    
                    curve_box = box_curve.column()
                    curve_box.enabled = context.scene.videomockup_curves.shadow_curve_toggle
                    curve_box.template_curve_mapping(rgb_shadow, "mapping", type='COLOR')

                    # Placeholder section
                    box_placeholder = layout.box()
                    col = box_placeholder.column()
                    col.alignment = 'CENTER'

                    toggle_text = "Placeholder ON" if context.scene.videomockup_curves.placeholder_enabled else "Placeholder OFF"
                    col.operator(
                        "videomockup.toggle_placeholder",
                        text=toggle_text,
                        icon='IMAGE_DATA',
                        depress=context.scene.videomockup_curves.placeholder_enabled
                    )

                    subcol = col.column()
                    subcol.enabled = context.scene.videomockup_curves.placeholder_enabled
                    row = subcol.row(align=True)
                    row.prop(context.scene.videomockup_curves, "placeholder_use_custom_color", text="")
                    row.label(text="Placeholder Color")
                    color_row = subcol.row()
                    color_row.enabled = context.scene.videomockup_curves.placeholder_use_custom_color
                    color_row.prop(context.scene.videomockup_curves, "placeholder_color", text="")

                    subcol = col.column()
                    subcol.enabled = context.scene.videomockup_curves.placeholder_enabled
                    row = subcol.row(align=True)
                    row.prop(context.scene.videomockup_curves, "placeholder_use_image", text="")
                    row.label(text="Placeholder Image")
                    img_row = subcol.row()
                    img_row.enabled = context.scene.videomockup_curves.placeholder_use_image
                    img_row.operator("videomockup.select_placeholder_image", text="Open", icon='FILEBROWSER')
                    
                    # Get image node and check if there's an image loaded
                    has_image = False
                    image_node = None
                    group = bpy.data.node_groups.get("All Previews")
                    if group:
                        hs_node = group.nodes.get("Highlight/Shadow Preview")
                        if hs_node and hs_node.node_tree:
                            hs_group = hs_node.node_tree
                            image_node = hs_group.nodes.get("Image")
                    
                    has_image = image_node and image_node.bl_idname == "CompositorNodeImage" and image_node.image
                    
                    if has_image:
                        row = col.row()
                        row.enabled = context.scene.videomockup_curves.placeholder_enabled and context.scene.videomockup_curves.placeholder_use_image
                        row.template_ID(image_node, "image", text="")
                    
                        box_transform = col.box()
                        box_transform.label(text="Transform Overlay", icon='OBJECT_ORIGIN')
                        box_transform.enabled = context.scene.videomockup_curves.placeholder_enabled and context.scene.videomockup_curves.placeholder_use_image
                        
                        row = box_transform.row(align=True)
                        row.prop(context.scene.videomockup_curves, "placeholder_transform_x", text="X")
                        
                        row = box_transform.row(align=True)
                        row.prop(context.scene.videomockup_curves, "placeholder_transform_y", text="Y")
                        
                        row = box_transform.row(align=True)
                        scale_slider = row.column()
                        scale_slider.scale_x = 0.8
                        scale_slider.prop(context.scene.videomockup_curves, "placeholder_transform_scale", text="Scale")
                        
                        button_row = row.row(align=True)
                        button_row.scale_x = 0.3
                        button_row.scale_y = 1
                        button_row.operator("videomockup.adjust_transform_scale", text="-").adjustment = -0.05
                        button_row.operator("videomockup.adjust_transform_scale", text="+").adjustment = 0.05
                        
                        row = box_transform.row(align=True)
                        row.prop(context.scene.videomockup_curves, "placeholder_transform_angle", text="Angle")

            # File Outputs section
            if group:
                box_output = layout.box()
                box_output.label(text="File Outputs", icon='FILE_TICK')

                output_nodes = [
                    ("File Output Image Render", "Image Render"),
                    ("File Output Background", "Background"),
                    ("File Output RGB", "RGB"),
                ]

                for node_name, label in output_nodes:
                    node = group.nodes.get(node_name)
                    if node:
                        is_enabled = not node.mute
                        icon = 'CHECKBOX_HLT' if is_enabled else 'CHECKBOX_DEHLT'

                        row = box_output.row()
                        row.operator(
                            "videomockup.toggle_file_output",
                            text=label,
                            icon=icon,
                            depress=is_enabled
                        ).node_name = node_name

                        row = box_output.row()
                        row.enabled = not node.mute
                        row.prop(node, "base_path", text="Output Path")
                    
    # Stacking Video header and content
    layout.prop(props, "show_vse_import", text="Stacked Video Output", emboss=True, icon='TRIA_DOWN' if props.show_vse_import else 'TRIA_RIGHT')

    if props.show_vse_import:
        box_import = layout.box()
        box_import.label(text="Import Image/Sequence", icon='SEQUENCE')

        # Check if sequences exist in channels 1 and 2
        seq_editor = context.scene.sequence_editor
        has_channel_1_sequence = False
        has_channel_2_sequence = False

        if seq_editor:
            for s in seq_editor.sequences:
                if s.channel == 1:
                    has_channel_1_sequence = True
                if s.channel == 2:
                    has_channel_2_sequence = True

        # Background button (channel 1)
        if has_channel_1_sequence:
            row = box_import.row(align=True)
            row.alignment = 'EXPAND'

            split = row.split(factor=0.7, align=True)

            inner = split.row(align=True)
            inner.label(text="", icon='CHECKMARK')
            inner.label(text="Background")

            split.operator("videomockup.remove_imported_sequence", text="", icon='X').channel = 1
        else:
            op_row = box_import.row()
            op_row.operator(
                "videomockup.add_image_strip",
                text="Background",
                icon='FILEBROWSER',
                depress=False
            ).channel = 1
            
        # RGB button (channel 2)
        if has_channel_2_sequence:
            row = box_import.row(align=True)
            row.alignment = 'EXPAND'

            split = row.split(factor=0.7, align=True)

            inner = split.row(align=True)
            inner.label(text="", icon='CHECKMARK')
            inner.label(text="RGB")

            split.operator("videomockup.remove_imported_sequence", text="", icon='X').channel = 2
        else:
            op_row = box_import.row()
            op_row.operator(
                "videomockup.add_image_strip",
                text="RGB",
                icon='FILEBROWSER',
                depress=False
            ).channel = 2
                
        box_adjust = layout.box()
        box_adjust.label(text="Adjustment Options", icon='SETTINGS')
        
        row = box_adjust.row(align=True)
        row.prop(props, "adjust_settings_enabled", text="")
        row.operator(
            "videomockup.toggle_adjust_settings",
            text="Adjust Settings",
            depress=props.adjust_settings_enabled
        )
        
        settings_box = box_adjust.column()
        settings_box.enabled = props.adjust_settings_enabled
        
        box_mp4 = layout.box()
        box_mp4.label(text="Render MP4 Video", icon='RENDER_ANIMATION')

        box_mp4.prop(props, "mp4_output_path", text="MP4 Output Path")

        render_row = box_mp4.row()
        render_row.scale_y = 1.5
        render_row.operator("videomockup.render_mp4", text="Render MP4 Video", icon='RENDER_ANIMATION')

class VIDEOMOCKUP_OT_select_placeholder_image(bpy.types.Operator):
    bl_idname = "videomockup.select_placeholder_image"
    bl_label = "Select Placeholder Image"
    bl_description = "Load an image to use as placeholder overlay"

    filepath: StringProperty(subtype="FILE_PATH")

    def execute(self, context):
        try:
            # Load the new image
            img = bpy.data.images.load(self.filepath)
        except:
            self.report({'ERROR'}, "Failed to load image.")
            return {'CANCELLED'}

        # Get the All Previews group
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            self.report({'ERROR'}, "Node group 'All Previews' not found.")
            return {'CANCELLED'}

        # First try to find Image node in All Previews group
        image_node = None
        hs_group = None
        
        # First, check for Image node in the Highlight/Shadow Preview group
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        if hs_node and hs_node.node_tree:
            hs_group = hs_node.node_tree
            image_node = hs_group.nodes.get("Image")
            
        # If not found, look in All Previews group
        if not image_node or image_node.bl_idname != "CompositorNodeImage":
            image_node = all_preview_group.nodes.get("Image")
            
        if not image_node or image_node.bl_idname != "CompositorNodeImage":
            self.report({'ERROR'}, "Image node not found in node groups.")
            return {'CANCELLED'}

        # Set the image to the Image node
        image_node.image = img
        
        # Make sure placeholder image is enabled
        props = context.scene.videomockup_curves
        props.placeholder_enabled = True
        props.placeholder_use_image = True
        props.placeholder_use_custom_color = False
        
        # ALWAYS reset transform values every time an image is loaded
        transform_node = hs_group.nodes.get("Transform")
        if transform_node:
            # Temporarily disable updates
            props._updating_transform = True
            
            # Reset to default values
            props.placeholder_transform_x = 0.0
            props.placeholder_transform_y = 0.0
            props.placeholder_transform_scale = 1.0
            props.placeholder_transform_angle = 0.0
            
            # Update the actual node values
            transform_node.inputs['X'].default_value = 0.0
            transform_node.inputs['Y'].default_value = 0.0
            transform_node.inputs['Scale'].default_value = 1.0
            transform_node.inputs['Angle'].default_value = 0.0
            
            # Re-enable updates
            props._updating_transform = False
        
        # Ensure image is connected to transform
        if image_node and transform_node:
            # First check if they're already connected
            is_connected = False
            for link in hs_group.links:
                if link.from_node == image_node and link.to_node == transform_node:
                    is_connected = True
                    break
                    
            # If not connected, make the connection
            if not is_connected:
                hs_group.links.new(image_node.outputs["Image"], transform_node.inputs["Image"])
        
        # Connect transform to alpha_over_ph and mix nodes
        alpha_over_ph = hs_group.nodes.get("Alpha Over PH")
        mix_highlight = hs_group.nodes.get("Mix")
        mix_shadow = hs_group.nodes.get("Mix.001")
        
        if transform_node and alpha_over_ph:
            # Remove existing connections to Alpha Over PH second input
            for link in list(hs_group.links):
                if link.to_node == alpha_over_ph and link.to_socket == alpha_over_ph.inputs[2]:
                    hs_group.links.remove(link)
                    
            # Connect Transform to Alpha Over PH
            hs_group.links.new(transform_node.outputs["Image"], alpha_over_ph.inputs[2])
            
        # Also connect to Mix nodes
        if transform_node and mix_highlight and mix_shadow:
            # Remove existing Mix connections
            for link in list(hs_group.links):
                if (link.to_node == mix_highlight and link.to_socket == mix_highlight.inputs[2]) or \
                   (link.to_node == mix_shadow and link.to_socket == mix_shadow.inputs[2]):
                    hs_group.links.remove(link)
            
            # Connect Transform to Mix nodes
            hs_group.links.new(transform_node.outputs["Image"], mix_highlight.inputs[2])
            hs_group.links.new(transform_node.outputs["Image"], mix_shadow.inputs[2])
        
        # Force a UI update
        for area in context.screen.areas:
            area.tag_redraw()
            
        # Force viewer node update by toggling and re-applying current settings
        current_output = context.scene.videomockup_selected_output
        # Switch to a different output and back to force update
        temp_output = "RGB" if current_output != "RGB" else "Image Render"
        bpy.ops.videomockup.switch_output(output_name=temp_output)
        bpy.ops.videomockup.switch_output(output_name=current_output)
        
        # Additional step: force a redraw of backdrop in Node Editor
        for area in context.screen.areas:
            if area.type == 'NODE_EDITOR':
                for space in area.spaces:
                    if space.type == 'NODE_EDITOR':
                        # Toggle backdrop
                        current_state = space.show_backdrop
                        space.show_backdrop = not current_state
                        space.show_backdrop = current_state
            elif area.type == 'IMAGE_EDITOR':
                # Force image editors to refresh too
                area.tag_redraw()
        
        self.report({'INFO'}, f"Loaded image: {img.name}")
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
class VIDEOMOCKUP_OT_toggle_placeholder(bpy.types.Operator):
    bl_idname = "videomockup.toggle_placeholder"
    bl_label = "Toggle Placeholder"
    bl_description = "Enable or disable the placeholder overlay"
    
    def execute(self, context):
        props = context.scene.videomockup_curves
        props.placeholder_enabled = not props.placeholder_enabled
        
        selected_output = context.scene.videomockup_selected_output
        all_preview_group = bpy.data.node_groups.get("All Previews")
        if not all_preview_group:
            self.report({'ERROR'}, "Node group 'All Previews' not found")
            return {'CANCELLED'}

        # Access the Highlight/Shadow Preview group to modify its nodes
        hs_node = all_preview_group.nodes.get("Highlight/Shadow Preview")
        if not hs_node or not hs_node.node_tree:
            self.report({'ERROR'}, "Highlight/Shadow Preview node or node tree not found")
            return {'CANCELLED'}
            
        hs_group = hs_node.node_tree
        
        # Get all the required nodes
        alpha_over_ph = hs_group.nodes.get("Alpha Over PH")  # Updated to use Alpha Over PH
        rgb_node = hs_group.nodes.get("RGB")
        transform_node = hs_group.nodes.get("Transform")
        group_input = next((n for n in hs_group.nodes if n.bl_idname == 'NodeGroupInput'), None)
        
        if not alpha_over_ph or not rgb_node or not transform_node or not group_input:
            self.report({'ERROR'}, "Required nodes not found in Highlight/Shadow Preview")
            return {'CANCELLED'}
        
        # Update the connection to Alpha Over PH based on placeholder state
        self.update_alpha_over_ph_connection(props, hs_group, alpha_over_ph, rgb_node, transform_node, group_input)
        
        # Update the viewer output selection
        bpy.ops.videomockup.switch_output(output_name=selected_output)
        
        return {'FINISHED'}
    
    def update_alpha_over_ph_connection(self, props, hs_group, alpha_over_ph, rgb_node, transform_node, group_input):
        # First, remove existing connection to Alpha Over PH second input (input[2])
        for link in list(hs_group.links):
            if link.to_node == alpha_over_ph and link.to_socket == alpha_over_ph.inputs[2]:
                hs_group.links.remove(link)
        
        # Connect based on whether the placeholder is enabled and what type
        if props.placeholder_enabled:
            if props.placeholder_use_image and transform_node.outputs[0].is_linked:
                # Connect transform node (if it has an image connected)
                hs_group.links.new(transform_node.outputs["Image"], alpha_over_ph.inputs[2])
            elif props.placeholder_use_custom_color:
                # Set RGB node color to match the placeholder color
                rgb_node.outputs[0].default_value = (
                    props.placeholder_color[0],
                    props.placeholder_color[1],
                    props.placeholder_color[2],
                    1.0
                )
                # Connect RGB node to Alpha Over PH
                hs_group.links.new(rgb_node.outputs["RGBA"], alpha_over_ph.inputs[2])
            else:
                # Default to DiffCol if neither image nor custom color is properly enabled
                hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_ph.inputs[2])
        else:
            # If placeholder is OFF, always use DiffCol
            hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_ph.inputs[2])
    
    def update_connections(self, props, hs_group, alpha_over_bg, rgb_node, transform_node, group_input,
                           mix_highlight, mix_shadow):
        # First, remove existing connections to Alpha Over BG second input
        for link in list(hs_group.links):
            if link.to_node == alpha_over_bg and link.to_socket == alpha_over_bg.inputs[2]:
                hs_group.links.remove(link)
                
        # Remove existing connections to input 2 of both mix nodes
        for link in list(hs_group.links):
            if (link.to_node == mix_highlight and link.to_socket == mix_highlight.inputs[2]) or \
               (link.to_node == mix_shadow and link.to_socket == mix_shadow.inputs[2]):
                hs_group.links.remove(link)
        
        # Connect based on current settings
        if props.placeholder_enabled:
            if props.placeholder_use_image:
                # Use the Transform node (which contains the image)
                hs_group.links.new(transform_node.outputs["Image"], alpha_over_bg.inputs[2])
                
                # Also connect transform node to Mix nodes for preview
                hs_group.links.new(transform_node.outputs["Image"], mix_highlight.inputs[2])
                hs_group.links.new(transform_node.outputs["Image"], mix_shadow.inputs[2])
                
            elif props.placeholder_use_custom_color:
                # Use the RGB node with custom color
                # First set the RGB node's color to match the placeholder color
                rgb_node.outputs[0].default_value = (
                    props.placeholder_color[0],
                    props.placeholder_color[1],
                    props.placeholder_color[2],
                    1.0
                )
                
                # Connect RGB node to Alpha Over BG
                hs_group.links.new(rgb_node.outputs["RGBA"], alpha_over_bg.inputs[2])
                
                # No connections for Mix nodes - they'll use their default values
            
            else:
                # Neither image nor custom color is enabled, use DiffCol as fallback
                hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_bg.inputs[2])
                hs_group.links.new(group_input.outputs["DiffCol"], mix_highlight.inputs[2])
                hs_group.links.new(group_input.outputs["DiffCol"], mix_shadow.inputs[2])
                
        else:
            # If placeholder is OFF, always connect DiffCol
            hs_group.links.new(group_input.outputs["DiffCol"], alpha_over_bg.inputs[2])
            hs_group.links.new(group_input.outputs["DiffCol"], mix_highlight.inputs[2])
            hs_group.links.new(group_input.outputs["DiffCol"], mix_shadow.inputs[2])
    
    def update_connections(self, props, hs_group, mix_highlight, mix_shadow, group_input, transform_node):
        # First, remove any existing connections to input 2 of both mix nodes
        for link in list(hs_group.links):
            if (link.to_node == mix_highlight and link.to_socket == mix_highlight.inputs[2]) or \
               (link.to_node == mix_shadow and link.to_socket == mix_shadow.inputs[2]):
                hs_group.links.remove(link)
        
        # Connect the appropriate input based on settings
        if props.placeholder_enabled:
            if props.placeholder_use_image:
                # Connect Transform output to both mix nodes' input 2
                hs_group.links.new(transform_node.outputs["Image"], mix_highlight.inputs[2])
                hs_group.links.new(transform_node.outputs["Image"], mix_shadow.inputs[2])
            elif props.placeholder_use_custom_color:
                # No connections needed - will use the default color value
                pass
            else:
                # Default behavior - connect DiffCol
                hs_group.links.new(group_input.outputs["DiffCol"], mix_highlight.inputs[2])
                hs_group.links.new(group_input.outputs["DiffCol"], mix_shadow.inputs[2])
        else:
            # When placeholder is OFF, always connect DiffCol
            hs_group.links.new(group_input.outputs["DiffCol"], mix_highlight.inputs[2])
            hs_group.links.new(group_input.outputs["DiffCol"], mix_shadow.inputs[2])

class VIDEOMOCKUP_OT_toggle_file_output(bpy.types.Operator):
    bl_idname = "videomockup.toggle_file_output"
    bl_label = "Toggle File Output"
    bl_description = "Enable or disable this file output node"

    node_name: bpy.props.StringProperty()

    def execute(self, context):
        group = bpy.data.node_groups.get("All Previews")
        if not group:
            self.report({'ERROR'}, "Node group 'All Previews' not found")
            return {'CANCELLED'}

        node = group.nodes.get(self.node_name)
        if node:
            node.mute = not node.mute
            return {'FINISHED'}

        self.report({'ERROR'}, f"Node '{self.node_name}' not found")
        return {'CANCELLED'}

def on_depsgraph_update(scene):
    """Update addon properties when transform node changes manually"""
    if scene != bpy.context.scene:
        return
        
    group = bpy.data.node_groups.get("All Previews")
    if not group:
        return
        
    transform_node = group.nodes.get("Transform")
    props = bpy.context.scene.videomockup_curves
    
    if transform_node and hasattr(props, "_updating_transform") and not props._updating_transform:
        # Only update if the properties exist and we're not already updating
        if hasattr(props, "placeholder_transform_x"):
            if props.placeholder_transform_x != transform_node.inputs['X'].default_value:
                props._updating_transform = True
                props.placeholder_transform_x = transform_node.inputs['X'].default_value
                props._updating_transform = False
                
        if hasattr(props, "placeholder_transform_y"):
            if props.placeholder_transform_y != transform_node.inputs['Y'].default_value:
                props._updating_transform = True
                props.placeholder_transform_y = transform_node.inputs['Y'].default_value
                props._updating_transform = False
                
        if hasattr(props, "placeholder_transform_scale"):
            if props.placeholder_transform_scale != transform_node.inputs['Scale'].default_value:
                props._updating_transform = True
                props.placeholder_transform_scale = transform_node.inputs['Scale'].default_value
                props._updating_transform = False
                
        if hasattr(props, "placeholder_transform_angle"):
            if props.placeholder_transform_angle != transform_node.inputs['Angle'].default_value:
                props._updating_transform = True
                props.placeholder_transform_angle = transform_node.inputs['Angle'].default_value
                props._updating_transform = False
    
class VIDEOMOCKUP_OT_add_image_strip(bpy.types.Operator):
    bl_idname = "videomockup.add_image_strip"
    bl_label = "Import Image/Sequence"
    bl_description = "Import an image sequence into the VSE"
    bl_options = {'REGISTER', 'UNDO'}

    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    files: bpy.props.CollectionProperty(type=bpy.types.OperatorFileListElement)
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.tif;*.tiff;*.tga", options={'HIDDEN'}
    )
    channel: bpy.props.IntProperty(default=1)  # Add channel property

    def execute(self, context):
        print("Executing add_image_strip")
        print(f"Files selected: {[f.name for f in self.files]}")
        print(f"Directory: {self.directory}")
        print(f"Target channel: {self.channel}")

        # Process the selected files
        import os

        allowed_exts = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".tga")
        file_names = [f.name for f in self.files if f.name.lower().endswith(allowed_exts)]
        if not file_names:
            self.report({'ERROR'}, "No supported image files selected.")
            return {'CANCELLED'}

        file_names.sort()
        total_files = len(file_names)
        print(f"Total image files: {total_files}")

        if not context.scene.sequence_editor:
            context.scene.sequence_editor_create()

        # Remove any existing strips in the specified channel
        for seq in list(context.scene.sequence_editor.sequences_all):
            if seq.channel == self.channel:
                context.scene.sequence_editor.sequences.remove(seq)
                
        # Create a new strip in the specified channel
        strip_name = "Background" if self.channel == 1 else "RGB"
        strip = context.scene.sequence_editor.sequences.new_image(
            name=strip_name,
            filepath=os.path.join(self.directory, file_names[0]),
            channel=self.channel,
            frame_start=context.scene.frame_current
        )
        
        # Add ALL remaining files to the strip
        # Make sure we iterate through all files, not just all-1
        for i in range(1, total_files):
            strip.elements.append(file_names[i])
        
        # Double-check that all files were added
        if len(strip.elements) != total_files:
            self.report({'WARNING'}, f"Expected {total_files} images, but added {len(strip.elements)}. Some frames may be missing.")
        else:
            self.report({'INFO'}, f"Successfully imported all {total_files} images to channel {self.channel}.")

        # Force all UI areas to redraw
        for area in context.screen.areas:
            area.tag_redraw()
            
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

class VIDEOMOCKUP_OT_remove_imported_sequence(bpy.types.Operator):
    bl_idname = "videomockup.remove_imported_sequence"
    bl_label = "Remove Imported Sequence"
    bl_description = "Remove sequence from specified channel"
    bl_options = {'REGISTER', 'UNDO'}
    
    channel: bpy.props.IntProperty(default=1)  # Add channel property
    
    def execute(self, context):
        seq_editor = context.scene.sequence_editor
        if not seq_editor:
            return {'CANCELLED'}
            
        # Find and remove any sequence in the specified channel
        for seq in list(seq_editor.sequences_all):
            if seq.channel == self.channel:
                seq_editor.sequences.remove(seq)
                
        # Force all UI areas to redraw
        for area in context.screen.areas:
            area.tag_redraw()
            
        return {'FINISHED'}

class VIDEOMOCKUP_OT_toggle_adjust_settings(bpy.types.Operator):
    bl_idname = "videomockup.toggle_adjust_settings"
    bl_label = "Toggle Adjust Settings"
    bl_description = "Enable or disable adjust settings mode"
    
    def execute(self, context):
        props = context.scene.videomockup_curves
        props.adjust_settings_enabled = not props.adjust_settings_enabled
        return {'FINISHED'}

class VIDEOMOCKUP_OT_render_mp4(bpy.types.Operator):
    bl_idname = "videomockup.render_mp4"
    bl_label = "Render MP4"
    bl_description = "Render the VSE timeline to high quality MP4 file"
    
    def execute(self, context):
        props = context.scene.videomockup_curves
        
        # Make sure handler isn't already registered
        if restore_render_settings in bpy.app.handlers.render_complete:
            bpy.app.handlers.render_complete.remove(restore_render_settings)
        if restore_render_settings in bpy.app.handlers.render_cancel:
            bpy.app.handlers.render_cancel.remove(restore_render_settings)
        
        # Store original render settings in scene properties for later restoration
        try:
            # Store original settings
            context.scene["vm_original_filepath"] = context.scene.render.filepath
            context.scene["vm_original_file_format"] = context.scene.render.image_settings.file_format
            context.scene["vm_original_use_sequencer"] = context.scene.render.use_sequencer
            context.scene["vm_original_use_compositing"] = context.scene.render.use_compositing
            context.scene["vm_original_constant_rate_factor"] = context.scene.render.ffmpeg.constant_rate_factor
            context.scene["vm_original_codec"] = context.scene.render.ffmpeg.codec
            context.scene["vm_original_format"] = context.scene.render.ffmpeg.format
            # Store color management settings
            context.scene["vm_original_view_transform"] = context.scene.view_settings.view_transform
            context.scene["vm_original_look"] = context.scene.view_settings.look
            
            # Set output format to FFmpeg video
            context.scene.render.image_settings.file_format = 'FFMPEG'
            
            # Process output path
            output_path = props.mp4_output_path
            
            # Check if the path is empty or doesn't have a filename/extension
            if not output_path or output_path == "//" or output_path.endswith(("\\", "/")):
                # Get the current blend filename without extension
                blend_filename = bpy.path.basename(bpy.data.filepath)
                if blend_filename:
                    # Remove .blend extension if present
                    blend_name = os.path.splitext(blend_filename)[0]
                    # Use blend file name for the MP4
                    output_path = os.path.join(output_path or "//", f"{blend_name}.mp4")
                else:
                    # If blend file is not saved, use default name
                    output_path = os.path.join(output_path or "//", "output.mp4")
            elif not output_path.lower().endswith('.mp4'):
                # Add .mp4 extension if missing
                output_path = output_path + ".mp4"
            
            # Set the output path from our property
            context.scene.render.filepath = output_path
            
            # Get absolute path for reporting and directory creation
            abs_path = bpy.path.abspath(output_path)
            print(f"Rendering MP4 to absolute path: {abs_path}")
            self.report({'INFO'}, f"Rendering to: {abs_path}")
            
            # Create output directory if it doesn't exist
            output_dir = os.path.dirname(abs_path)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)
                print(f"Created output directory: {output_dir}")
            
            # Set to render only VSE timeline, not compositor
            context.scene.render.use_sequencer = True
            context.scene.render.use_compositing = False
            
            # Set H.264 settings based on quality selection
            context.scene.render.ffmpeg.format = 'MPEG4'
            context.scene.render.ffmpeg.codec = 'H264'
            
            # Set the constant rate factor based on quality selection
            if hasattr(props, 'mp4_quality'):
                if props.mp4_quality == 'HIGH':
                    context.scene.render.ffmpeg.constant_rate_factor = 'HIGH'
                elif props.mp4_quality == 'MEDIUM':
                    context.scene.render.ffmpeg.constant_rate_factor = 'MEDIUM'
                else:  # LOW
                    context.scene.render.ffmpeg.constant_rate_factor = 'LOW'
            else:
                # Default to high quality if property doesn't exist
                context.scene.render.ffmpeg.constant_rate_factor = 'HIGH'
                
            # Disable audio
            context.scene.render.ffmpeg.audio_codec = 'NONE'
            
            # Set color management to Standard view transform with no Look
            context.scene.view_settings.view_transform = 'Standard'
            context.scene.view_settings.look = 'None'
            
            # Register the handler to restore settings after render completes
            bpy.app.handlers.render_complete.append(restore_render_settings)
            bpy.app.handlers.render_cancel.append(restore_render_settings)
            
            # Start the render animation
            bpy.ops.render.render('INVOKE_DEFAULT', animation=True)
            
            quality_text = getattr(props, 'mp4_quality', 'HIGH').lower()
            self.report({'INFO'}, f"Rendering {quality_text} quality MP4 from sequencer to: {abs_path}")
            return {'FINISHED'}
        except Exception as e:
            # If there's an error, try to restore settings immediately
            self.restore_on_error(context)
            self.report({'ERROR'}, f"Render failed: {str(e)}")
            return {'CANCELLED'}

# Define as global function
def restore_render_settings(scene):
    # Restore original settings after render is completed or canceled
    try:
        # We need to access the scene properties where we stored the original values
        if "vm_original_filepath" in scene:
            # Restore all the original settings
            scene.render.filepath = scene["vm_original_filepath"] 
            scene.render.image_settings.file_format = scene["vm_original_file_format"]
            scene.render.use_sequencer = scene["vm_original_use_sequencer"]
            scene.render.use_compositing = scene["vm_original_use_compositing"]
            scene.render.ffmpeg.constant_rate_factor = scene["vm_original_constant_rate_factor"]
            scene.render.ffmpeg.codec = scene["vm_original_codec"]
            scene.render.ffmpeg.format = scene["vm_original_format"]
            # Restore color management settings
            scene.view_settings.view_transform = scene["vm_original_view_transform"]
            scene.view_settings.look = scene["vm_original_look"]
            # Turn off Adjust Settings
            if hasattr(scene, 'videomockup_curves'):
                scene.videomockup_curves.adjust_settings_enabled = False
            
            # Clean up stored properties
            del scene["vm_original_filepath"]
            del scene["vm_original_file_format"]
            del scene["vm_original_use_sequencer"]
            del scene["vm_original_use_compositing"]
            del scene["vm_original_constant_rate_factor"]
            del scene["vm_original_codec"] 
            del scene["vm_original_format"]
            del scene["vm_original_view_transform"]
            del scene["vm_original_look"]
            
            print("Successfully restored original render settings")
        else:
            print("No original settings found to restore")
    except Exception as e:
        print(f"Error restoring render settings: {str(e)}")
    
    # Remove the handler regardless of success or failure
    if restore_render_settings in bpy.app.handlers.render_complete:
        bpy.app.handlers.render_complete.remove(restore_render_settings)
    if restore_render_settings in bpy.app.handlers.render_cancel:
        bpy.app.handlers.render_cancel.remove(restore_render_settings)

class VIDEOMOCKUP_OT_adjust_transform_scale(bpy.types.Operator):
    bl_idname = "videomockup.adjust_transform_scale"
    bl_label = "Adjust Scale"
    bl_description = "Make a precise adjustment to the scale"
    bl_options = {'REGISTER', 'UNDO'}
    
    adjustment: bpy.props.FloatProperty(default=0.1)
    
    def execute(self, context):
        props = context.scene.videomockup_curves
        new_value = max(0.001, props.placeholder_transform_scale + self.adjustment)
        props.placeholder_transform_scale = new_value
        return {'FINISHED'}

def register():
    register_properties()
    bpy.utils.register_class(VIDEOMOCKUP_OT_toggle_highlight_curve)
    bpy.utils.register_class(VIDEOMOCKUP_OT_toggle_shadow_curve)
    bpy.utils.register_class(VIDEOMOCKUP_OT_add_nodes)
    bpy.utils.register_class(VIDEOMOCKUP_OT_cryptomatte_object)
    bpy.utils.register_class(VIDEOMOCKUP_OT_cryptomatte_material)
    bpy.utils.register_class(VIDEOMOCKUP_PT_node_editor)
    bpy.utils.register_class(VIDEOMOCKUP_PT_image_editor)
    bpy.utils.register_class(VIDEOMOCKUP_PT_sequencer)
    bpy.utils.register_class(VIDEOMOCKUP_OT_save_viewer_image)
    bpy.utils.register_class(VIDEOMOCKUP_OT_switch_output)
    bpy.utils.register_class(VIDEOMOCKUP_OT_select_placeholder_image)
    bpy.utils.register_class(VIDEOMOCKUP_OT_toggle_placeholder)
    bpy.utils.register_class(VIDEOMOCKUP_OT_toggle_file_output)
    bpy.utils.register_class(VIDEOMOCKUP_OT_add_image_strip)
    bpy.utils.register_class(VIDEOMOCKUP_OT_remove_imported_sequence)
    bpy.utils.register_class(VIDEOMOCKUP_OT_toggle_adjust_settings)
    bpy.utils.register_class(VIDEOMOCKUP_OT_render_mp4)
    bpy.utils.register_class(VIDEOMOCKUP_OT_adjust_transform_scale)
    
def unregister():
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_adjust_transform_scale)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_render_mp4)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_toggle_adjust_settings)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_remove_imported_sequence)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_add_image_strip)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_toggle_file_output)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_toggle_placeholder)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_select_placeholder_image)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_switch_output)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_save_viewer_image)
    bpy.utils.unregister_class(VIDEOMOCKUP_PT_image_editor)
    bpy.utils.unregister_class(VIDEOMOCKUP_PT_sequencer)
    bpy.utils.unregister_class(VIDEOMOCKUP_PT_node_editor)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_cryptomatte_material)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_cryptomatte_object)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_add_nodes)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_toggle_shadow_curve)
    bpy.utils.unregister_class(VIDEOMOCKUP_OT_toggle_highlight_curve)
    
    unregister_properties()

# Keep this part as is
if __name__ == "__main__":

    register()
