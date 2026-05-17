#!/usr/bin/env python3
"""
Fixed Handler for Wolfram Alpha computational knowledge engine queries.

This version bypasses the buggy wap library and uses direct HTTP requests
with proper XML parsing for better reliability.

Capabilities:
    - Mathematical computations
    - Scientific calculations
    - Data analysis
    - Knowledge-based queries
    - Image result handling
    - Pod-based information
    - Multi-format responses
    - Technical computations
    - Natural language processing
    - Educational content
    - Research assistance
    - Data visualization

Patterns:
    - "calculate {expression}"
    - "solve {equation}"
    - "analyze {data}"
    - "convert {units}"
    - "compare {items}"
    - "find {information}"
    - "plot {function}"
    - "explain {concept}"

Intents:
    - wolfram_calculate
    - wolfram_solve
    - wolfram_analyze
    - wolfram_convert
    - wolfram_compare
    - wolfram_find
    - wolfram_plot
    - wolfram_explain

Parameters:
    - query: string (input query)
    - format: string (response format)
    - include_pods: List[string]
    - exclude_pods: List[string]
    - max_width: integer
    - plot_width: integer
    - timeout: integer
"""

import urllib.request
import urllib.parse
from xml.dom import minidom
from pathlib import Path
import logging

# Import BaseHandler
from Handler.handler_base import BaseHandler

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load API Key
current_dir = Path(__file__).resolve().parent
API_KEY_FILE = current_dir.parent / "API" / "WOLFRAM_API_KEY.txt"

if not API_KEY_FILE.exists():
    raise FileNotFoundError(f"Wolfram API Key file not found. Please ensure the file exists at '{API_KEY_FILE}'.")

with open(API_KEY_FILE, "r") as f:
    WOLFRAM_API_KEY = f.read().strip()

# Wolfram|Alpha API Server (HTTPS)
WOLFRAM_SERVER = "https://api.wolframalpha.com/v2/query"

class WolframHandler(BaseHandler):
    """Enhanced Wolfram Alpha handler with better error handling and reliability."""
    
    def __init__(self, api_key=None):
        self.handler_name = "wolfram"
        self.app_name = "Wolfram Alpha Handler"
        self.api_key = api_key or WOLFRAM_API_KEY
        self.base_url = WOLFRAM_SERVER
        self.pod_types_data = self._load_pod_types_data()
        self.intent_patterns = self._load_intent_patterns()
    
    def get_mcp_tools(self):
        """Return detailed MCP tool definitions with proper parameter schemas and usage examples"""
        return [
            {
                "name": "query_wolfram_alpha",
                "description": """Perform sophisticated queries to Wolfram|Alpha with full blob/image/math support.

MATHEMATICAL QUERIES:
- Calculations: "2+2", "integrate sin(x) from 0 to pi", "derivative of x^2"  
- Equations: "solve x^2 - 4 = 0", "x + y = 5, x - y = 1"
- Plots: "plot sin(x)", "graph y = x^2 from -5 to 5"

SCIENTIFIC QUERIES:
- Physics: "speed of light", "mass of electron", "E=mc^2 where m=10kg"
- Chemistry: "molecular weight of water", "periodic table element 6"
- Biology: "DNA structure", "human genome size"

CONVERSIONS:
- Units: "100 miles to kilometers", "32 fahrenheit to celsius"
- Currency: "100 USD to EUR", "convert 500 yen to dollars"

DATA ANALYSIS:
- Statistics: "mean of {1,2,3,4,5}", "standard deviation of dataset"
- Probability: "probability of rolling a 6", "binomial distribution"

KNOWLEDGE QUERIES:
- Geography: "population of Tokyo", "distance between NYC and LA"
- History: "when was Einstein born", "World War 2 dates"

BLOB/IMAGE HANDLING:
- Set format_type='image' for visual results (graphs, plots, diagrams)
- Results include full image metadata: src, alt, title, width, height
- Supports MathML for mathematical expressions
- Pod-based results with detailed subpod information""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Natural language query for math, science, conversions, data analysis, or knowledge"},
                        "format_type": {"type": "string", "description": "Response format: 'plaintext' (text), 'image' (visual/graphs), 'mathml' (math markup)", "default": "plaintext"},
                        "include_pods": {"type": "array", "items": {"type": "string"}, "description": "Pod types to include: 'Result', 'Plot', 'Solution', 'Derivative', 'Properties', etc."},
                        "exclude_pods": {"type": "array", "items": {"type": "string"}, "description": "Pod types to exclude from results"},
                        "timeout": {"type": "integer", "description": "Request timeout in seconds", "default": 30},
                        "max_width": {"type": "integer", "description": "Maximum pixel width for generated images/plots", "default": 500}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_enhanced_result",
                "description": """Get enhanced Wolfram results with automatic pod prediction and intelligent filtering.

AUTOMATIC FEATURES:
- Predicts likely pod types based on query patterns
- Filters results to most relevant information
- Optimizes for faster, focused responses

BEST FOR:
- Complex queries where you want Wolfram to automatically determine the most relevant results
- When you need focused results without manually specifying pod types
- Research queries where relevance filtering helps

EXAMPLE USAGE:
- get_enhanced_result("solve differential equation dy/dx = y") → auto-predicts mathematical solution pods
- get_enhanced_result("population growth model") → auto-predicts statistical and demographic pods""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Any mathematical, scientific, or knowledge query"},
                        "auto_filter": {"type": "boolean", "description": "Enable intelligent pod filtering based on query analysis", "default": True}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_optimized_result", 
                "description": """Get optimized Wolfram results using advanced intent classification and pod prediction.

OPTIMIZATION FEATURES:
- Classifies query intent (mathematical, scientific, conversion, financial, geographical, statistical)
- Maps intents to optimal pod types
- Combines intent analysis with pod prediction for best results
- Returns optimization metadata showing the reasoning process

INTENT TYPES SUPPORTED:
- Mathematical: equations, calculus, algebra, geometry
- Scientific: physics, chemistry, biology data
- Conversion: units, currency, measurements  
- Financial: calculations, interest, economics
- Geographical: locations, distances, demographics
- Statistical: probability, distributions, data analysis

EXAMPLE USAGE:
- Mathematical: "integrate cos(x) dx" → optimized for derivative/solution pods
- Scientific: "boiling point of water" → optimized for properties/classification pods
- Conversion: "50 mph to km/h" → optimized for conversion/unit pods""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Query that will benefit from intent-based optimization"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "classify_query_intent",
                "description": """Classify and analyze the intent of queries for optimal Wolfram processing.

CLASSIFICATION CATEGORIES:
- mathematical: algebra, calculus, equations, geometry
- conversion: units, currency, measurements, transformations  
- scientific: physics, chemistry, biology, research data
- financial: economics, interest calculations, market data
- geographical: locations, distances, population, coordinates
- statistical: probability, distributions, data analysis
- general: mixed or unclear intent

RETURNS:
- primary_intent: the main category detected
- confidence: scoring based on keyword matches  
- all_intents: complete analysis with scores
- optimization_suggestions: recommended pod types and parameters

EXAMPLE RESULTS:
- "solve x^2 = 4" → mathematical intent, high confidence
- "100 USD to EUR" → conversion intent, high confidence
- "population of Paris weather today" → mixed geographical intent""",
                "input_schema": {
                    "type": "object", 
                    "properties": {
                        "query": {"type": "string", "description": "Query text to analyze for mathematical, scientific, conversion, or other intent patterns"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "predict_pod_types",
                "description": """Predict optimal pod types for queries to get the most relevant Wolfram results.

PREDICTION LOGIC:
- Analyzes query keywords and patterns
- Matches against historical successful pod combinations
- Suggests specific pod types likely to contain answers
- Helps optimize include_pods parameter for main query

COMMON POD TYPES:
- Result: Direct answers and calculations
- Plot: Graphs, charts, visualizations  
- Solution: Step-by-step equation solutions
- Derivative: Calculus operations
- Properties: Scientific/mathematical properties
- Classification: Categorization and identification
- Unit information: Conversion details
- Basic statistics: Statistical analysis
- Geographic properties: Location data

USAGE:
- Use before query_wolfram_alpha() to optimize pod selection
- Helps reduce irrelevant information in responses
- Particularly useful for complex multi-part queries""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Query to analyze for optimal pod type prediction"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "filter_pods_by_type",
                "description": """Filter raw Wolfram XML responses to extract only specific pod types.

FILTERING CAPABILITIES:
- Processes raw XML from Wolfram Alpha API
- Extracts only pods matching specified types
- Preserves full pod structure and metadata
- Useful for post-processing large result sets

COMMON FILTER SCENARIOS:
- Extract only 'Result' pods for direct answers
- Get only 'Plot' pods for visualizations
- Filter to 'Properties' + 'Classification' for scientific data
- Combine 'Solution' + 'Derivative' for math work

XML STRUCTURE PRESERVED:
- Pod titles and IDs
- Subpod content and metadata  
- Image links and properties
- MathML expressions""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "xml_content": {"type": "string", "description": "Raw XML response from Wolfram Alpha API"},
                        "desired_pod_types": {"type": "array", "items": {"type": "string"}, "description": "Pod type names to extract: 'Result', 'Plot', 'Solution', 'Properties', etc."}
                    },
                    "required": ["xml_content", "desired_pod_types"]
                }
            },
            {
                "name": "format_result_by_intent",
                "description": """Format and structure Wolfram results based on detected query intent for optimal presentation.

INTENT-BASED FORMATTING:
- mathematical: Highlights equations, solutions, step-by-step work
- conversion: Emphasizes converted values and unit details  
- scientific: Prioritizes properties, classifications, data
- financial: Focuses on calculations and economic metrics
- geographical: Highlights location data and geographic properties
- statistical: Emphasizes statistical analysis and distributions

ENHANCED RESULT STRUCTURE:
- Creates intent-specific result keys (e.g., 'converted_value', 'solution_steps')
- Prioritizes most relevant information for the intent type
- Maintains original data while adding structured access
- Improves readability for specific use cases

EXAMPLE TRANSFORMATIONS:
- Math query → 'primary_answer', 'solution_steps' keys added
- Conversion → 'converted_value', 'unit_details' highlighted
- Science → 'scientific_properties', 'classification' featured""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "result": {"type": "object", "description": "Raw result data from any Wolfram query method"},
                        "intent_type": {"type": "string", "description": "Intent classification: mathematical, conversion, scientific, financial, geographical, statistical"}
                    },
                    "required": ["result", "intent_type"]
                }
            },
            {
                "name": "get_pod_titles",
                "description": """Get list of available pod titles for a query - useful for debugging and pod discovery.

DEBUGGING USES:
- See what information categories Wolfram found
- Identify available pod types for include_pods parameter
- Understand result structure before full query
- Troubleshoot why certain information isn't appearing

RETURNS: List of pod titles like:
- ['Input interpretation', 'Result', 'Number line', 'Root plot', 'Solution']
- ['Input interpretation', 'Result', 'Unit conversions', 'Comparison'] 
- ['Basic information', 'Properties', 'Structure diagram', 'Classification']

WORKFLOW:
1. Run get_pod_titles() to see available pods
2. Use interesting pod names in include_pods for main query
3. Get focused results with only the pods you want""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Any query to analyze for available pod types"}
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_simple_answer",
                "description": """Get just the simple, direct answer from Wolfram without images, plots, or complex formatting.

STREAMLINED RESULTS:
- Returns only the main answer text
- No blob/image data or complex pod structures  
- No plots, diagrams, or visual elements
- Fastest response for simple calculations

BEST FOR:
- Quick calculations: "2+2", "sqrt(16)"
- Simple conversions: "100 miles to km"  
- Basic facts: "population of Tokyo"
- When you only need the direct answer

RETURNS: Plain text answer or None if no simple answer found
- "4" (for 2+2)
- "160.934 kilometers" (for 100 miles to km)
- "13.96 million people" (for Tokyo population)""",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Simple query where you only want the direct answer"}
                    },
                    "required": ["query"]
                }
            }
        ]
    
    def handle(self, action, **kwargs):
        """Required handle method for BaseHandler compatibility."""
        if action == "query" or action == "wolfram_query":
            query = kwargs.get("query", kwargs.get("text", ""))
            if not query:
                return {"error": "No query provided"}
            return self.query_wolfram_alpha(query, **kwargs)
        else:
            return {"error": f"Unknown action: {action}"}
        
    def _load_intent_patterns(self):
        """Load intent patterns for better query classification."""
        try:
            intents_file = current_dir.parent / "Intents" / "intents_wolfram.json"
            if intents_file.exists():
                import json
                with open(intents_file, 'r') as f:
                    data = json.load(f)
                    return data.get('WOLFRAM_QUERY', {}).get('examples', [])
            else:
                logger.warning(f"Intent patterns file not found at {intents_file}")
                return []
        except Exception as e:
            logger.error(f"Error loading intent patterns: {e}")
            return []
    
    def classify_query_intent(self, query):
        """Classify query intent based on patterns and examples."""
        query_lower = query.lower()
        intent_scores = {}
        
        # Mathematical operations
        math_keywords = ['solve', 'calculate', 'integrate', 'derivative', 'equation', 'roots']
        if any(keyword in query_lower for keyword in math_keywords):
            intent_scores['mathematical'] = len([k for k in math_keywords if k in query_lower])
        
        # Unit conversions
        conversion_keywords = ['to', 'in', 'convert', 'celsius', 'fahrenheit', 'miles', 'kilometers']
        if any(keyword in query_lower for keyword in conversion_keywords):
            intent_scores['conversion'] = len([k for k in conversion_keywords if k in query_lower])
        
        # Scientific queries
        science_keywords = ['atom', 'molecular', 'electron', 'energy', 'force', 'speed', 'wavelength']
        if any(keyword in query_lower for keyword in science_keywords):
            intent_scores['scientific'] = len([k for k in science_keywords if k in query_lower])
        
        # Financial calculations
        finance_keywords = ['interest', 'mortgage', 'payment', 'stock', 'price', 'compound']
        if any(keyword in query_lower for keyword in finance_keywords):
            intent_scores['financial'] = len([k for k in finance_keywords if k in query_lower])
        
        # Geographic/factual information
        geo_keywords = ['population', 'capital', 'height', 'distance', 'coordinates']
        if any(keyword in query_lower for keyword in geo_keywords):
            intent_scores['geographical'] = len([k for k in geo_keywords if k in query_lower])
        
        # Probability/statistics
        stats_keywords = ['probability', 'dice', 'distribution', 'correlation', 'confidence']
        if any(keyword in query_lower for keyword in stats_keywords):
            intent_scores['statistical'] = len([k for k in stats_keywords if k in query_lower])
        
        # Return the intent with highest score
        if intent_scores:
            primary_intent = max(intent_scores.items(), key=lambda x: x[1])
            return {
                'primary_intent': primary_intent[0],
                'confidence': primary_intent[1],
                'all_intents': intent_scores
            }
        else:
            return {
                'primary_intent': 'general',
                'confidence': 1,
                'all_intents': {'general': 1}
            }
    
    def _load_pod_types_data(self):
        """Load the pod types data for enhanced result parsing."""
        try:
            pod_types_file = current_dir.parent / "wolfram_pod_types.json"
            if pod_types_file.exists():
                import json
                with open(pod_types_file, 'r') as f:
                    return json.load(f)
            else:
                logger.warning(f"Pod types file not found at {pod_types_file}")
                return {}
        except Exception as e:
            logger.error(f"Error loading pod types data: {e}")
            return {}
    
    def predict_pod_types(self, query):
        """Predict likely pod types for a given query based on patterns."""
        predicted_pods = []
        query_lower = query.lower()
        
        for pod_type, examples in self.pod_types_data.items():
            for example in examples:
                # Simple keyword matching - could be enhanced with ML
                example_words = set(example.lower().split())
                query_words = set(query_lower.split())
                
                # If query shares significant words with example, predict this pod type
                if len(example_words.intersection(query_words)) >= 2:
                    if pod_type not in predicted_pods:
                        predicted_pods.append(pod_type)
                        break
        
        return predicted_pods
    
    def filter_pods_by_type(self, xml_content, desired_pod_types):
        """Filter XML response to only include specified pod types."""
        try:
            doc = minidom.parseString(xml_content)
            pods = doc.getElementsByTagName('pod')
            
            filtered_pods = []
            for pod in pods:
                pod_title = pod.getAttribute('title')
                if any(desired_type.lower() in pod_title.lower() for desired_type in desired_pod_types):
                    filtered_pods.append(pod)
            
            return filtered_pods
        except Exception as e:
            logger.error(f"Error filtering pods: {e}")
            return []
    
    def get_enhanced_result(self, query, auto_filter=True):
        """Get enhanced results with automatic pod type prediction and filtering."""
        try:
            # Predict likely pod types for better results
            if auto_filter and self.pod_types_data:
                predicted_pods = self.predict_pod_types(query)
                if predicted_pods:
                    logger.info(f"Predicted pod types for '{query}': {predicted_pods}")
                    # Use predicted pod types to optimize the query
                    include_pods = predicted_pods[:5]  # Limit to top 5 predictions
                else:
                    include_pods = None
            else:
                include_pods = None
            
            # Perform the query
            result = self.query_wolfram_alpha(query, include_pods=include_pods)
            
            # Add metadata about prediction
            if isinstance(result, dict) and predicted_pods:
                result['predicted_pod_types'] = predicted_pods
                result['enhanced_parsing'] = True
            
            return result
            
        except Exception as e:
            logger.error(f"Error in enhanced result generation: {e}")
            return self.query_wolfram_alpha(query)  # Fallback to regular query
    
    def get_optimized_result(self, query):
        """Get optimized results using both intent classification and pod type prediction."""
        try:
            # Classify the query intent
            intent_data = self.classify_query_intent(query)
            primary_intent = intent_data['primary_intent']
            
            # Map intents to preferred pod types for filtering
            intent_pod_mapping = {
                'mathematical': ['Result', 'Root plot', 'Derivative', 'Solution', 'Plot'],
                'conversion': ['Result', 'Conversion', 'Unit information'],
                'scientific': ['Result', 'Scientific notation', 'Properties', 'Classification'],
                'financial': ['Result', 'Financial computation', 'Economic data'],
                'geographical': ['Result', 'Geographic properties', 'Current result'],
                'statistical': ['Result', 'Statistical plots', 'Distribution plots', 'Basic statistics'],
                'general': ['Input interpretation', 'Result', 'Basic information']
            }
            
            # Get preferred pod types for this intent
            preferred_pods = intent_pod_mapping.get(primary_intent, ['Result'])
            
            # Combine with pod type prediction
            predicted_pods = self.predict_pod_types(query)
            
            # Merge and prioritize pod types
            optimized_pods = list(set(preferred_pods + predicted_pods))
            
            logger.info(f"Query: '{query}' | Intent: {primary_intent} | Optimized pods: {optimized_pods}")
            
            # Perform optimized query
            result = self.query_wolfram_alpha(query, include_pods=optimized_pods)
            
            # Add optimization metadata
            if isinstance(result, dict):
                result['optimization_data'] = {
                    'intent_classification': intent_data,
                    'predicted_pods': predicted_pods,
                    'preferred_pods': preferred_pods,
                    'optimized_pods': optimized_pods
                }
            
            return result
            
        except Exception as e:
            logger.error(f"Error in optimized result generation: {e}")
            return self.query_wolfram_alpha(query)  # Fallback to regular query
    
    def format_result_by_intent(self, result, intent_type):
        """Format results based on the detected intent for better presentation."""
        if not isinstance(result, dict):
            return result
            
        formatted_result = result.copy()
        
        # Intent-specific formatting
        if intent_type == 'mathematical':
            # Prioritize mathematical results
            if 'Result' in result:
                formatted_result['primary_answer'] = result['Result']
            if 'Solution' in result:
                formatted_result['solution_steps'] = result['Solution']
                
        elif intent_type == 'conversion':
            # Highlight conversion results
            if 'Result' in result:
                formatted_result['converted_value'] = result['Result']
            if 'Unit information' in result:
                formatted_result['unit_details'] = result['Unit information']
                
        elif intent_type == 'scientific':
            # Show scientific properties
            if 'Properties' in result:
                formatted_result['scientific_properties'] = result['Properties']
            if 'Classification' in result:
                formatted_result['classification'] = result['Classification']
                
        elif intent_type == 'financial':
            # Emphasize financial calculations
            if 'Financial computation' in result:
                formatted_result['financial_result'] = result['Financial computation']
                
        elif intent_type == 'geographical':
            # Highlight geographic data
            if 'Geographic properties' in result:
                formatted_result['geographic_info'] = result['Geographic properties']
                
        elif intent_type == 'statistical':
            # Show statistical analysis
            if 'Basic statistics' in result:
                formatted_result['statistics'] = result['Basic statistics']
            
        return formatted_result
        
    def query_wolfram_alpha(self, query, format_type='plaintext', include_pods=None, 
                          exclude_pods=None, timeout=30, max_width=500):
        """
        Perform a query to Wolfram|Alpha and return results.
        
        Args:
            query (str): The input query string
            format_type (str): Response format ('plaintext', 'image', 'mathml')
            include_pods (List[str]): Specific pods to include
            exclude_pods (List[str]): Specific pods to exclude
            timeout (int): Request timeout in seconds
            max_width (int): Maximum width for images
            
        Returns:
            dict: Processed results, including plaintext and images
        """
        try:
            # Construct parameters
            params = {
                'input': query,
                'appid': self.api_key,
                'format': format_type
            }
            
            # Add optional parameters
            if include_pods:
                params['includepodid'] = ','.join(include_pods)
            if exclude_pods:
                params['excludepodid'] = ','.join(exclude_pods)
            if max_width:
                params['width'] = str(max_width)
                
            # Construct URL
            query_string = urllib.parse.urlencode(params)
            full_url = f"{self.base_url}?{query_string}"
            
            logger.debug(f"Querying Wolfram Alpha: {query}")
            
            # Make request
            request = urllib.request.Request(full_url)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = response.read().decode('utf-8')
                
            # Parse XML response
            parsed_results = self._parse_wolfram_response(result)
            
            logger.info(f"Successfully processed query: '{query}' - {len(parsed_results)} pods")
            return parsed_results
            
        except urllib.error.HTTPError as e:
            logger.error(f"HTTP error querying Wolfram Alpha: {e.code} - {e.reason}")
            return None
        except urllib.error.URLError as e:
            logger.error(f"URL error querying Wolfram Alpha: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"Error querying Wolfram Alpha: {str(e)}")
            return None
    
    def _parse_wolfram_response(self, xml_response):
        """
        Parse the XML response from Wolfram Alpha.
        
        Args:
            xml_response (str): Raw XML response
            
        Returns:
            dict: Parsed results organized by pod
        """
        try:
            # Parse XML
            dom = minidom.parseString(xml_response)
            
            # Check if query was successful
            queryresult = dom.getElementsByTagName('queryresult')[0]
            success = queryresult.getAttribute('success')
            error = queryresult.getAttribute('error')
            
            if success != 'true':
                logger.warning(f"Wolfram query not successful: success={success}, error={error}")
                return {}
            
            # Extract pods
            parsed_results = {}
            pods = dom.getElementsByTagName('pod')
            
            for pod in pods:
                pod_title = pod.getAttribute('title')
                pod_id = pod.getAttribute('id')
                pod_results = []
                
                # Extract subpods
                subpods = pod.getElementsByTagName('subpod')
                for subpod in subpods:
                    subpod_data = {}
                    
                    # Extract plaintext
                    plaintext_nodes = subpod.getElementsByTagName('plaintext')
                    if plaintext_nodes and plaintext_nodes[0].firstChild:
                        subpod_data['plaintext'] = plaintext_nodes[0].firstChild.nodeValue
                    
                    # Extract images
                    img_nodes = subpod.getElementsByTagName('img')
                    if img_nodes:
                        img_data = {
                            'src': img_nodes[0].getAttribute('src'),
                            'alt': img_nodes[0].getAttribute('alt'),
                            'title': img_nodes[0].getAttribute('title'),
                            'width': img_nodes[0].getAttribute('width'),
                            'height': img_nodes[0].getAttribute('height')
                        }
                        subpod_data['img'] = img_data
                    
                    # Extract MathML if present
                    mathml_nodes = subpod.getElementsByTagName('mathml')
                    if mathml_nodes:
                        subpod_data['mathml'] = mathml_nodes[0].toxml()
                    
                    pod_results.append(subpod_data)
                
                parsed_results[pod_title] = {
                    'id': pod_id,
                    'subpods': pod_results
                }
            
            return parsed_results
            
        except Exception as e:
            logger.error(f"Error parsing Wolfram response: {str(e)}")
            return {}
    
    def get_pod_titles(self, query):
        """Get just the pod titles for a query (useful for debugging)."""
        results = self.query_wolfram_alpha(query)
        if results:
            return list(results.keys())
        return []
    
    def get_simple_answer(self, query):
        """Get just the simple answer for a query."""
        results = self.query_wolfram_alpha(query)
        if results:
            # Look for Result pod first
            if 'Result' in results:
                subpods = results['Result']['subpods']
                if subpods and 'plaintext' in subpods[0]:
                    return subpods[0]['plaintext']
            
            # Fall back to first pod with plaintext
            for pod_title, pod_data in results.items():
                if pod_data['subpods']:
                    first_subpod = pod_data['subpods'][0]
                    if 'plaintext' in first_subpod:
                        return first_subpod['plaintext']
        
        return None

# Create global handler instance
wolfram_handler = WolframHandler()

# Maintain backward compatibility with original function name
def query_wolfram_alpha(query, **kwargs):
    """
    Backward compatibility function for existing code.
    
    Args:
        query (str): The input query string
        **kwargs: Additional parameters
        
    Returns:
        dict: Processed results in the original format
    """
    results = wolfram_handler.query_wolfram_alpha(query, **kwargs)
    
    if not results:
        return None
    
    # Convert to original format for backward compatibility
    converted_results = {}
    for pod_title, pod_data in results.items():
        pod_results = []
        for subpod in pod_data['subpods']:
            converted_subpod = {
                'plaintext': subpod.get('plaintext'),
                'img_src': subpod.get('img', {}).get('src')
            }
            pod_results.append(converted_subpod)
        converted_results[pod_title] = pod_results
    
    return converted_results

# MCP Server Integration
class WolframMCPHandler:
    """MCP-compatible handler for Wolfram Alpha queries."""
    
    def __init__(self):
        self.handler = WolframHandler()
        self.name = "wolfram"
        self.description = "Wolfram Alpha computational knowledge engine"
        
    async def handle_query(self, query: str, **kwargs) -> dict:
        """Handle a query request via MCP."""
        try:
            result = self.handler.query_wolfram_alpha(query, **kwargs)
            return {
                'status': 'success',
                'data': result,
                'query': query
            }
        except Exception as e:
            return {
                'status': 'error',
                'error': str(e),
                'query': query
            }
    
    def get_capabilities(self) -> dict:
        """Return handler capabilities for MCP registration."""
        return {
            'name': self.name,
            'description': self.description,
            'methods': [
                'query_wolfram_alpha',
                'get_simple_answer',
                'get_pod_titles'
            ],
            'parameters': {
                'query': {'type': 'string', 'required': True},
                'format_type': {'type': 'string', 'default': 'plaintext'},
                'include_pods': {'type': 'list', 'default': None},
                'exclude_pods': {'type': 'list', 'default': None},
                'timeout': {'type': 'integer', 'default': 30},
                'max_width': {'type': 'integer', 'default': 500}
            }
        }

if __name__ == "__main__":
    # Test the handler
    test_queries = [
        "2 + 2",
        "solve x^2 - 4 = 0",
        "derivative of sin(x)",
        "population of New York City",
        "convert 100 miles to kilometers"
    ]
    
    for query in test_queries:
        print(f"\nTesting: {query}")
        result = query_wolfram_alpha(query)
        if result:
            print(f"✓ Success - {len(result)} pods")
            for pod_title, pod_content in list(result.items())[:2]:  # First 2 pods
                print(f"  {pod_title}:")
                for subpod in pod_content[:1]:  # First subpod
                    if subpod.get('plaintext'):
                        print(f"    {subpod['plaintext']}")
        else:
            print("✗ Failed")